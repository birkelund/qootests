#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Minimal qtest wire-protocol client and QEMU runner.

Boots a QEMU binary with '-accel qtest' and speaks the qtest protocol over
a unix socket. Note that the test side is the socket *server*: we create
the socket first and QEMU's '-qtest unix:PATH' chardev connects to us as
a client.

There is no BIOS with qtest, so PCI devices must be configured manually
through the config space access ports (0xcf8/0xcfc on x86).

Usage:

    with QemuQTest(['-M', 'pc', ...], workdir='/tmp/xyz') as q:
        q.pci_cfg_write32(0x80002010, 0xfe000000)   # BAR0
        q.pci_cfg_write32(0x80002004, 0x07)         # mem + bus master
        val = q.readl(0xfe000000)

The QEMU binary defaults to ../qemu/build/qemu-system-x86_64 relative to
this file, overridable with the QOOTEST_QEMU environment variable.

QEMU stderr is captured to '<workdir>/qemu-stderr.log' and included in
error messages (a QEMU crash shows up as the connection going away).
"""
import base64
import os
import signal
import socket
import subprocess
import tempfile
import time

_QEMU_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '..', 'qemu', 'build', 'qemu-system-x86_64')

PCI_CONF_ADDR = 0xcf8
PCI_CONF_DATA = 0xcfc


class QTestError(Exception):
    pass


class QemuQTest:
    """A QEMU instance driven over the qtest protocol."""

    def __init__(self, qemu_args, workdir=None, qemu_binary=None):
        self.qemu_binary = (qemu_binary or
                            os.environ.get('QOOTEST_QEMU') or _QEMU_DEFAULT)
        if not os.path.isfile(self.qemu_binary):
            raise QTestError(f'QEMU binary not found: {self.qemu_binary} '
                             '(set QOOTEST_QEMU)')
        self.qemu_args = ['-display', 'none', '-accel', 'qtest'] + list(qemu_args)
        self.workdir = workdir or tempfile.mkdtemp(prefix='qootest-')
        self.sock_path = os.path.join(self.workdir, 'qtest.sock')
        self.stderr_path = os.path.join(self.workdir, 'qemu-stderr.log')
        self.proc = None
        self._srv = None
        self._sock = None
        self._stderr_file = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self, timeout=10.0):
        """Start QEMU and wait for it to connect to the qtest socket."""
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        srv.listen(1)
        srv.settimeout(timeout)
        self._srv = srv

        self._stderr_file = open(self.stderr_path, 'wb')
        self.proc = subprocess.Popen(
            [self.qemu_binary, '-qtest', f'unix:{self.sock_path}'] + self.qemu_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_file,
            cwd=os.path.dirname(os.path.abspath(self.qemu_binary)))

        try:
            self._sock, _ = srv.accept()
        except socket.timeout:
            self.stop(raise_on_error=False)
            raise QTestError('QEMU did not connect to the qtest socket within '
                             f'{timeout}s\n{self.stderr_tell()}')
        self._sock.settimeout(timeout)
        return self

    def stop(self, raise_on_error=True):
        """Shut QEMU down and close the socket."""
        try:
            if self._sock is not None:
                self._sock.close()
                self._sock = None
            if self._srv is not None:
                self._srv.close()
                self._srv = None
        finally:
            if self._stderr_file is not None:
                self._stderr_file.close()
                self._stderr_file = None

        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
            rc = self.proc.returncode
            if rc != 0 and raise_on_error:
                raise QTestError(f'QEMU exited with status {rc}\n'
                                 + self.stderr_tell())

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        # do not mask an in-flight test failure with a shutdown error
        self.stop(raise_on_error=(exc_type is None))
        return False

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------

    def stderr_tell(self, limit=4096):
        """Return the tail of QEMU's stderr log."""
        try:
            with open(self.stderr_path, 'rb') as f:
                f.seek(0, 2)
                sz = min(f.tell(), limit)
                f.seek(-sz, 2)
                return f.read().decode(errors='replace').rstrip()
        except OSError:
            return '<qemu stderr unavailable>'

    # ------------------------------------------------------------------
    # wire protocol
    # ------------------------------------------------------------------

    def cmd(self, c, timeout=10.0):
        """Send a qtest command, return the response line (without 'OK ')."""
        if self._sock is None:
            raise QTestError('not started')
        self._sock.settimeout(timeout)
        self._sock.sendall(c.encode() + b'\n')
        buf = b''
        while b'\n' not in buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise QTestError(f'qtest connection closed by QEMU while '
                                 f'waiting for response to "{c}"\n'
                                 + self.stderr_tell())
            buf += chunk
        resp = buf.split(b'\n', 1)[0].strip().decode()
        if not resp.startswith('OK'):
            raise QTestError(f'qtest command "{c}" failed: {resp}')
        return resp

    def _read(self, name, addr):
        resp = self.cmd(f'{name} 0x{addr:x}')
        return int(resp.split()[1], 16)

    def readb(self, addr):
        return self._read('readb', addr)

    def readw(self, addr):
        return self._read('readw', addr)

    def readl(self, addr):
        return self._read('readl', addr)

    def readq(self, addr):
        return self._read('readq', addr)

    def _write(self, name, addr, value):
        self.cmd(f'{name} 0x{addr:x} 0x{value:x}')

    def writeb(self, addr, value):
        self._write('writeb', addr, value)

    def writew(self, addr, value):
        self._write('writew', addr, value)

    def writel(self, addr, value):
        self._write('writel', addr, value)

    def writeq(self, addr, value):
        self._write('writeq', addr, value)

    # x86 PCI config space access via the conf1 mechanism
    def pci_cfg_write32(self, cfg_addr, value):
        self.cmd(f'outl 0x{PCI_CONF_ADDR:x} 0x{cfg_addr:x}')
        self.cmd(f'outl 0x{PCI_CONF_DATA:x} 0x{value:x}')

    def pci_cfg_read32(self, cfg_addr):
        self.cmd(f'outl 0x{PCI_CONF_ADDR:x} 0x{cfg_addr:x}')
        return int(self.cmd(f'inl 0x{PCI_CONF_DATA:x}').split()[1], 16)

    # ------------------------------------------------------------------
    # bulk memory access
    # ------------------------------------------------------------------

    def mem_write(self, addr, data):
        """Write bytes to guest RAM (qtest 'write' command)."""
        self.cmd(f'write 0x{addr:x} 0x{len(data):x} 0x{data.hex()}')

    def mem_zero(self, addr, length):
        """
        Zero guest RAM. The 'write' command requires a payload of at least
        3 characters, so we pass '0x0' which expands to zeros.
        """
        self.cmd(f'write 0x{addr:x} 0x{length:x} 0x0')

    def mem_read(self, addr, length):
        """Read bytes from guest RAM (qtest 'b64read' command)."""
        resp = self.cmd(f'b64read 0x{addr:x} 0x{length:x}')
        return base64.b64decode(resp.split(' ', 1)[1])

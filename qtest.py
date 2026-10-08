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
        self._buf = b''
        # asynchronous events, e.g. ('raise', 20) for an 'IRQ raise 20'
        # message emitted after irq_intercept_in; see QemuQTest.cmd()
        self.irq_events = []

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
        self._buf = b''
        self.irq_events = []
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

    def _read_line(self, timeout):
        """Read one line from the qtest socket, buffering partial messages.

        The buffer persists across commands so that bytes received past
        the line we need (e.g. an asynchronous message that arrived in
        the same recv() as a response) are never lost.
        """
        while b'\n' not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise QTestError('qtest connection closed by QEMU\n'
                                 + self.stderr_tell())
            self._buf += chunk
        line, self._buf = self._buf.split(b'\n', 1)
        return line.strip().decode()

    def cmd(self, c, timeout=10.0):
        """Send a qtest command, return the response line (without 'OK').

        QEMU may send asynchronous messages at any point, e.g.
        'IRQ raise N' / 'IRQ lower N' after 'irq_intercept_in' has been
        issued. Those are collected in self.irq_events (a list of
        ('raise'|'lower', gsi) tuples in arrival order) and reading
        continues until the response to this command arrives: the
        request/response pairing is never disturbed by an interleaved
        event.
        """
        if self._sock is None:
            raise QTestError('not started')
        self._sock.settimeout(timeout)
        self._sock.sendall(c.encode() + b'\n')
        while True:
            resp = self._read_line(timeout)
            if resp.startswith('IRQ raise ') or resp.startswith('IRQ lower '):
                _, kind, num = resp.split()
                self.irq_events.append((kind, int(num)))
                continue
            if not resp.startswith('OK'):
                raise QTestError(f'qtest command "{c}" failed: {resp}')
            return resp

    # ------------------------------------------------------------------
    # irq event handling (see cmd())
    # ------------------------------------------------------------------

    def pop_irq_event(self, kind):
        """Pop the first collected IRQ event of `kind` ('raise'/'lower'),
        or None if there is none. Other events are left in place."""
        for ev in self.irq_events:
            if ev[0] == kind:
                self.irq_events.remove(ev)
                return ev
        return None

    def wait_irq(self, kind, timeout=5.0):
        """Wait for an IRQ event of `kind`, pumping the QEMU main loop with
        harmless memory reads (which also drives bottom halves, so
        asynchronous completions get posted). Returns (kind, gsi)."""
        ev = self.pop_irq_event(kind)
        if ev is not None:
            return ev
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.001)
            self.cmd('readl 0', timeout=1.0)
            ev = self.pop_irq_event(kind)
            if ev is not None:
                return ev
        raise TimeoutError(f'no IRQ "{kind}" event within {timeout}s '
                           f'(collected so far: {self.irq_events})')

    def assert_no_irq(self, timeout=0.1):
        """Pump for `timeout` seconds and assert that no IRQ events arrived."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.001)
            self.cmd('readl 0', timeout=1.0)
        if self.irq_events:
            raise AssertionError(f'unexpected IRQ events: {self.irq_events}')

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

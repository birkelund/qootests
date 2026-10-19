#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
NVMe helper layer for the qootests collection.

Wraps QemuQTest with:

  * PCI BAR setup (qtest has no BIOS to do it for us)
  * controller enable/disable (CC.EN handshake, RDY polling)
  * submission/completion queue objects with CID-matched CQE polling so
    tests need no fixed sleeps and stay robust when queues are re-created
    over CQ memory containing stale phase-1 CQEs from a previous
    controller reset
  * 64-byte command builders for the admin and I/O commands used by the
    tests

Command result: wait_cqe()/run() return (result, sc) where sc is the
16-bit status code from the CQE *including* the DNR bit (e.g. LBA Range
with DNR is 0x4080). Names match include/block/nvme.h.
"""
import os
import struct
import time
from uuid import UUID

from qtest import QemuQTest

# default guest memory layout used by the tests (pc machine, 128MB RAM)
ASQ_ADDR = 0x100000
ACQ_ADDR = 0x200000
BUF_ADDR = 0x300000          # scratch buffer for identify etc.
IO_SQ_ADDR = 0x500000
IO_CQ_ADDR = 0x600000
DATA_ADDR = 0x700000         # data buffer for I/O commands
DBS_ADDR = 0x900000          # shadow doorbell buffer (DBBUF) guest memory
EIS_ADDR = 0x980000          # shadow event index buffer (DBBUF) guest memory
DB_BASE = 0x1000             # queue doorbell area starts at BAR0 + 0x1000

BAR0_DEFAULT = 0xfe000000

# controller register offsets
REG_CAP = 0x00
REG_VS = 0x08
REG_INTMS = 0x0c
REG_INTMC = 0x10
REG_CC = 0x14
REG_CSTS = 0x1c
REG_NSSR = 0x20
REG_AQA = 0x24
REG_ASQ = 0x28
REG_ACQ = 0x30

# NvmeIdentifyController field offsets
ID_CTRL_VID = 0
ID_CTRL_CNTLID = 78           # controller id (after sn/mn/fr/rab/ieee/cmic/mdts)
ID_CTRL_OACS = 256           # optional admin command support
ID_CTRL_AERL = 259           # asynchronous event request limit (0-based)
ID_CTRL_MDTS = 77            # max data transfer size (in MPSMIN pages)
ID_CTRL_SQES = 512           # sqes[0] = min | max << 4
ID_CTRL_CQES = 513
ID_CTRL_NN = 516
ID_CTRL_ONCS = 520

# OACS bits
NVME_OACS_FORMAT = 1 << 1
NVME_OACS_NS_MGMT = 1 << 3
NVME_OACS_DBCS = 1 << 8

# NvmeIdentifyNamespace field offsets
ID_NS_NSZE = 0
ID_NS_FLBAS = 26
ID_NS_LBAF = 128             # lbaf[i] (4B): ms:2, ds:1, rp:1
ID_NS_DS = ID_NS_LBAF + 2    # lba data size exponent in lbaf[flbas & 0xf]

# admin opcodes
ADM_DELETE_SQ = 0x00
ADM_CREATE_SQ = 0x01
ADM_GET_LOG_PAGE = 0x02
ADM_DELETE_CQ = 0x04
ADM_CREATE_CQ = 0x05
ADM_IDENTIFY = 0x06
ADM_ABORT = 0x08
ADM_SET_FEATURES = 0x09
ADM_GET_FEATURES = 0x0a
ADM_AER = 0x0c
ADM_NS_ATTACHMENT = 0x15
ADM_DBBUF_CONFIG = 0x7c
ADM_FORMAT = 0x80

# io opcodes
CMD_FLUSH = 0x00
CMD_WRITE = 0x01
CMD_READ = 0x02
CMD_WRITE_ZEROES = 0x08
CMD_DSM = 0x09

# status codes (generic command status, include/block/nvme.h)
ST_SUCCESS = 0x0000
ST_INVALID_OPCODE = 0x0001
ST_INVALID_FIELD = 0x0002
ST_INVALID_NSID = 0x000b
NSID_BROADCAST = 0xffffffff
ST_LBA_RANGE = 0x0080
ST_INVALID_CQID = 0x0100
ST_INVALID_QID = 0x0101
ST_INVALID_QUEUE_DEL = 0x010c
ST_DATA_TRAS_ERROR = 0x0004
ST_NS_ALREADY_ATTACHED = 0x0118
ST_NS_NOT_ATTACHED = 0x011a
ST_NS_CTRL_LIST_INVALID = 0x011c
ST_DNR = 0x4000

# Namespace Attachment (0x15) selection values (cdw10 & 0xf)
NS_ATTACHMENT_ATTACH = 0x0
NS_ATTACHMENT_DETACH = 0x1

# feature identifiers
FEAT_NUM_QUEUES = 0x07

# identify controller / namespace cns values
CNS_NS = 0x00
CNS_CTRL = 0x01
CNS_ACTIVE_LIST = 0x02
CNS_DESCR_LIST = 0x03

# Frozen namespace UUID for QEMU-derived namespace UUIDs (RFC 4122
# version 5; must match hw/nvme/core/ns.c).
NVME_NS_UUID_NAMESPACE = UUID('e0746fe0-2a28-5b3a-81ff-d3b35cce6101')

# Namespace Identifier Descriptor types (Identify CNS 0x03)
NIDT_EUI64 = 0x01
NIDT_NGUID = 0x02
NIDT_UUID = 0x03
NIDT_CSI = 0x04

# CC and CSTS bits
CC_EN = 1 << 0
CC_IOSQES = 0x6 << 16    # 64-byte submission queue entries
CC_IOCQES = 0x4 << 20    # 16-byte completion queue entries
CSTS_RDY = 1 << 0

# PCI config space addresses for the device under test at 04.0
PCI_CFG = 0x80000000 | (4 << 11)
PCI_CFG_BAR0 = PCI_CFG + 0x10
PCI_CFG_BAR2 = PCI_CFG + 0x18
PCI_CFG_CMD = PCI_CFG + 0x04
PCI_CMD_MEM = 0x02
PCI_CMD_MASTER = 0x04

# CMB/PMR BAR0 register offsets (NvmeBar layout, see include/block/nvme.h)
REG_CMBLOC = 0x38
REG_CMBSZ = 0x3c
REG_CMBMSC = 0x50
REG_CMBSTS = 0x58
REG_PMRCAP = 0xe00
REG_PMRCTL = 0xe04
REG_PMRMSCL = 0xe14
REG_PMRMSCU = 0xe18


def check(actual, expected, what):
    """Assert that `actual == expected`, with a readable message."""
    if actual != expected:
        raise AssertionError(f'{what}: got 0x{actual:x}, expected 0x{expected:x}')


def make_disk(workdir, size=1 << 30):
    """Create a sparse raw disk image and return its path."""
    path = os.path.join(workdir, 'disk.img')
    with open(path, 'wb') as f:
        f.truncate(size)
    return path


# ----------------------------------------------------------------------
# command builders: each returns a fresh 64-byte command
# ----------------------------------------------------------------------
# NvmeCmd layout (little endian): opcode@0, flags@1, cid@2, nsid@4,
# mptr@0x10, dptr (prp1)@0x18, prp2@0x20, cdw10@0x28 .. cdw15@0x3c

def build_identify(cns, nsid=0, prp1=0):
    c = bytearray(64)
    c[0] = ADM_IDENTIFY
    struct.pack_into('<I', c, 4, nsid)
    struct.pack_into('<Q', c, 0x18, prp1)
    struct.pack_into('<I', c, 0x28, cns)
    return c


def build_get_features(fid, cdw11=0):
    c = bytearray(64)
    c[0] = ADM_GET_FEATURES
    struct.pack_into('<I', c, 0x28, fid)
    struct.pack_into('<I', c, 0x2c, cdw11)
    return c


def build_create_cq(cqid, cq_addr, qsize, irq_vec=0, ie=False):
    c = bytearray(64)
    c[0] = ADM_CREATE_CQ
    # cdw10: qid | (qsize - 1) << 16, cdw11: PC (phys contiguous) | IE |
    # vec << 16 -- IE (bit 1) enables interrupts for the queue
    cdw11 = 0x01 | (irq_vec << 16)
    if ie:
        cdw11 |= 0x02
    struct.pack_into('<I', c, 0x28, cqid | ((qsize - 1) << 16))
    struct.pack_into('<I', c, 0x2c, cdw11)
    struct.pack_into('<Q', c, 0x18, cq_addr)
    return c


def build_create_sq(sqid, cqid, sq_addr, qsize):
    c = bytearray(64)
    c[0] = ADM_CREATE_SQ
    struct.pack_into('<I', c, 0x28, sqid | ((qsize - 1) << 16))
    struct.pack_into('<I', c, 0x2c, 0x01 | (cqid << 16))
    struct.pack_into('<Q', c, 0x18, sq_addr)
    return c


def build_delete_sq(sqid):
    c = bytearray(64)
    c[0] = ADM_DELETE_SQ
    struct.pack_into('<I', c, 0x28, sqid)
    return c


def build_delete_cq(cqid):
    c = bytearray(64)
    c[0] = ADM_DELETE_CQ
    struct.pack_into('<I', c, 0x28, cqid)
    return c


def build_rw(op, nsid, slba, nlb, prp1=0, prp2=0):
    c = bytearray(64)
    c[0] = op
    struct.pack_into('<I', c, 4, nsid)
    struct.pack_into('<Q', c, 0x18, prp1)
    struct.pack_into('<Q', c, 0x20, prp2)
    struct.pack_into('<Q', c, 0x28, slba)
    # cdw12: nlb - 1 (0-based count)
    struct.pack_into('<I', c, 0x30, (nlb - 1) & 0xffff)
    return c


def build_flush(nsid):
    c = bytearray(64)
    c[0] = CMD_FLUSH
    struct.pack_into('<I', c, 4, nsid)
    return c


def build_write_zeroes(nsid, slba, nlb):
    c = bytearray(64)
    c[0] = CMD_WRITE_ZEROES
    struct.pack_into('<I', c, 4, nsid)
    struct.pack_into('<Q', c, 0x28, slba)
    struct.pack_into('<I', c, 0x30, (nlb - 1) & 0xffff)
    return c


def build_dbbuf_config(dbs_addr, eis_addr):
    """Doorbell Buffer Config (0x7c): PRP1 = shadow doorbell buffer,
    PRP2 = shadow event index buffer."""
    c = bytearray(64)
    c[0] = ADM_DBBUF_CONFIG
    struct.pack_into('<Q', c, 0x18, dbs_addr)
    struct.pack_into('<Q', c, 0x20, eis_addr)
    return c


def build_dsm(nsid, prp1, nr, attrs=0x04):
    """Dataset Management; attrs=0x04 is the deallocate (AD) bit."""
    c = bytearray(64)
    c[0] = CMD_DSM
    struct.pack_into('<I', c, 4, nsid)
    struct.pack_into('<Q', c, 0x18, prp1)
    struct.pack_into('<I', c, 0x28, nr - 1)   # cdw10: nr - 1
    struct.pack_into('<I', c, 0x2c, attrs)
    return c


def build_ns_attachment(sel, nsid, prp1):
    """Namespace Attachment (0x15); prp1 is the controller list buffer:
    a 4096-byte array of uint16, element 0 being the number of
    controller ids that follow."""
    c = bytearray(64)
    c[0] = ADM_NS_ATTACHMENT
    struct.pack_into('<I', c, 4, nsid)
    struct.pack_into('<Q', c, 0x18, prp1)
    struct.pack_into('<I', c, 0x28, sel & 0xf)
    return c


class NvmeQueue:
    """
    A single submission/completion queue pair. The admin pair (sqid 0) is
    an instance of this too.

    CQE waiting: poll the CQ slot for a CID match instead of sleeping.
    CIDs are allocated monotonically per device (never reused until 64k
    commands), so a stale CQE left in CQ memory by a queue incarnation
    from before a controller reset can never match.
    """

    def __init__(self, dev, sq_addr, cq_addr, sq_db, cq_db, qsize=16):
        self.dev = dev
        self.sq_addr = sq_addr
        self.cq_addr = cq_addr
        self.sq_db = sq_db
        self.cq_db = cq_db
        self.qsize = qsize
        self.sq_tail = 0
        self.cq_head = 0
        self.phase = 1
        self.pending = []

    def submit(self, cmd):
        """Place a command in the SQ and ring the doorbell."""
        cmd = bytearray(cmd)
        cid = self.dev.next_cid()
        struct.pack_into('<H', cmd, 2, cid)
        self.dev.q.mem_write(self.sq_addr + self.sq_tail * 64, cmd)
        self.sq_tail = (self.sq_tail + 1) % self.qsize
        self.dev.q.writel(self.sq_db, self.sq_tail)
        self.pending.append(cid)
        return cid

    def wait_cqe(self, timeout=5.0):
        """Wait for and consume the next CQE.

        Completions may arrive out of submission order (independent
        commands may complete in any order), so the CQE at the head slot
        is matched against any outstanding cid -- not the oldest.
        """
        if not self.pending:
            raise AssertionError('wait_cqe() with no outstanding command')
        slot = self.cq_addr + self.cq_head * 16
        deadline = time.time() + timeout
        while True:
            got_cid = self.dev.q.readw(slot + 12)
            if got_cid in self.pending:
                break
            if time.time() > deadline:
                raise TimeoutError(f'timed out waiting for a CQE with cids '
                                   f'{self.pending} (got cid 0x{got_cid:x})')
            time.sleep(0.001)
        self.pending.remove(got_cid)
        status = self.dev.q.readw(slot + 14)
        result = self.dev.q.readl(slot)
        # phase bit must match our tracked phase; this doubles as a device
        # regression check for the phase toggle behaviour
        check(status & 1, self.phase, 'CQE phase bit')
        sc = (status >> 1) & 0xffff

        self.cq_head = (self.cq_head + 1) % self.qsize
        if self.cq_head == 0:
            self.phase ^= 1
        self.dev.q.writel(self.cq_db, self.cq_head)
        return result, sc

    def run(self, cmd, timeout=5.0):
        """Submit a command and wait for its completion."""
        self.submit(cmd)
        return self.wait_cqe(timeout)


class NvmeDev:
    """A single NVMe device (default: vnvme) in a qtest-driven QEMU."""

    def __init__(self, disk, workdir, device='vnvme', serial='foo',
                 qemu_binary=None, qemu_args=None, pre_args=None,
                 device_opts='', machine='pc'):
        # a comma-separated list of device options; ';'-separated shell
        # metacharacters are rejected (the string goes into argv).
        assert ';' not in device_opts
        assert not device_opts.startswith(',') and not device_opts.endswith(',')
        opts = f',{device_opts}' if device_opts else ''
        args = ['-M', machine,
                '-drive', f'if=none,id=drv0,file={disk},format=raw']
        if pre_args:
            # placed before the -device line: options that must exist
            # at device realization time (e.g. -object memory-backend
            # referenced by a memdev property)
            args += pre_args
        args += ['-device', f'{device},drive=drv0,serial={serial},'
                            f'addr=04.0{opts}']
        if qemu_args:
            args += qemu_args
        self.serial = serial
        self.q = QemuQTest(args, workdir=workdir, qemu_binary=qemu_binary)
        self.bar0 = None
        self.aq = None
        self._cid = 0

    # lifecycle ---------------------------------------------------------

    def __enter__(self):
        self.q.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.q.stop(raise_on_error=(exc_type is None))
        return False

    def next_cid(self):
        cid = self._cid
        self._cid = (self._cid + 1) & 0xffff
        return cid

    @property
    def cid(self):
        """The cid the next submitted command will be assigned."""
        return self._cid

    def set_next_cid(self, cid):
        """Advance the cid counter (monotonically; never rewinds).

        Migration tests reconstruct a driver from a source snapshot: the
        cids must continue the source's sequence, otherwise a fresh cid
        may collide with the cids already committed to migrated (or
        still zeroed) completion queue memory and be matched against
        stale contents.
        """
        self._cid = max(self._cid, cid & 0xffff)

    # pci / controller registers ----------------------------------------

    def setup_pci(self, bar0=BAR0_DEFAULT):
        """Map BAR0 and enable memory access + bus mastering."""
        self.bar0 = bar0
        self.q.pci_cfg_write32(PCI_CFG_BAR0, bar0)
        self.q.pci_cfg_write32(PCI_CFG_CMD, PCI_CMD_MEM | PCI_CMD_MASTER)

    def reg_read(self, off):
        return self.q.readl(self.bar0 + off)

    def reg_write(self, off, val):
        self.q.writel(self.bar0 + off, val)

    def enable(self, asq=ASQ_ADDR, acq=ACQ_ADDR, qsize=16, timeout=5.0):
        """
        Transition the controller to enabled (CC.EN=0 -> 1), wait for RDY.
        Returns a fresh admin NvmeQueue.
        """
        # zero the queue memory so stale CQEs from a previous incarnation
        # (before a reset) cannot confuse the CID matcher -- belt and
        # suspenders; cids are monotonic anyway.
        self.q.mem_zero(asq, qsize * 64)
        self.q.mem_zero(acq, qsize * 16)
        self.reg_write(REG_AQA, ((qsize - 1) << 16) | (qsize - 1))
        self.reg_write(REG_ASQ, asq)
        self.reg_write(REG_ACQ, acq)
        self.reg_write(REG_CC, CC_EN | CC_IOSQES | CC_IOCQES)
        self._poll_rdy(1, timeout)
        self.aq = NvmeQueue(self, asq, acq,
                             self.bar0 + DB_BASE, self.bar0 + DB_BASE + 4,
                             qsize)
        return self.aq

    def disable(self, timeout=5.0):
        """CC.EN=0, wait for RDY to clear."""
        cc = self.reg_read(REG_CC) & ~CC_EN
        self.reg_write(REG_CC, cc)
        self._poll_rdy(0, timeout)
        self.aq = None

    def is_rdy(self):
        return bool(self.reg_read(REG_CSTS) & CSTS_RDY)

    def _poll_rdy(self, want, timeout):
        deadline = time.time() + timeout
        while self.is_rdy() != bool(want):
            if time.time() > deadline:
                raise TimeoutError(f'RDY did not become {want}')
            time.sleep(0.001)

    # io queue lifecycle --------------------------------------------------

    def create_io_queue(self, qid, sq_addr=IO_SQ_ADDR, cq_addr=IO_CQ_ADDR,
                        qsize=16, irq_vec=0, irq_en=False):
        """Create CQ `qid` then SQ `qid`, return an NvmeQueue for the pair."""
        self.q.mem_zero(sq_addr, qsize * 64)
        self.q.mem_zero(cq_addr, qsize * 16)
        _, sc = self.aq.run(build_create_cq(qid, cq_addr, qsize, irq_vec,
                                           ie=irq_en))
        check(sc, ST_SUCCESS, f'Create CQ {qid}')
        _, sc = self.aq.run(build_create_sq(qid, qid, sq_addr, qsize))
        check(sc, ST_SUCCESS, f'Create SQ {qid}')
        sq_db = self.bar0 + DB_BASE + 2 * qid * 4
        cq_db = self.bar0 + DB_BASE + (2 * qid + 1) * 4
        return NvmeQueue(self, sq_addr, cq_addr, sq_db, cq_db, qsize)

    def delete_io_queue(self, qid):
        _, sc = self.aq.run(build_delete_sq(qid))
        check(sc, ST_SUCCESS, f'Delete SQ {qid}')
        _, sc = self.aq.run(build_delete_cq(qid))
        check(sc, ST_SUCCESS, f'Delete CQ {qid}')

    # identify / features --------------------------------------------------

    def identify_ctrl(self):
        """Return the 4096-byte Identify Controller structure."""
        self.q.mem_zero(BUF_ADDR, 4096)
        _, sc = self.aq.run(build_identify(CNS_CTRL, prp1=BUF_ADDR))
        check(sc, ST_SUCCESS, 'Identify Controller')
        return self.q.mem_read(BUF_ADDR, 4096)

    def identify_ns(self, nsid=1):
        """Return the 4096-byte Identify Namespace structure for `nsid`."""
        self.q.mem_zero(BUF_ADDR, 4096)
        _, sc = self.aq.run(build_identify(CNS_NS, nsid=nsid, prp1=BUF_ADDR))
        check(sc, ST_SUCCESS, f'Identify Namespace {nsid}')
        return self.q.mem_read(BUF_ADDR, 4096)

    def identify_ns_list(self, min_nsid=0):
        """Return the 4096-byte Active Namespace id list (CNS 0x02)."""
        self.q.mem_zero(BUF_ADDR, 4096)
        _, sc = self.aq.run(build_identify(CNS_ACTIVE_LIST, nsid=min_nsid,
                                           prp1=BUF_ADDR))
        check(sc, ST_SUCCESS, f'Identify Active Namespace List ({min_nsid})')
        return self.q.mem_read(BUF_ADDR, 4096)

    def identify_ns_descr_list(self, nsid=1):
        """Return the 4096-byte Namespace Descriptor List (CNS 0x03)."""
        self.q.mem_zero(BUF_ADDR, 4096)
        _, sc = self.aq.run(build_identify(CNS_DESCR_LIST, nsid=nsid,
                                           prp1=BUF_ADDR))
        check(sc, ST_SUCCESS, f'Identify Namespace Descriptor List ({nsid})')
        return self.q.mem_read(BUF_ADDR, 4096)

    def ns_attachment(self, sel, nsid, ctrl_ids):
        """Namespace Attachment (0x15): attach (`sel` =
        NS_ATTACHMENT_ATTACH) or detach (`NS_ATTACHMENT_DETACH`)
        namespace `nsid` to/from the controllers in `ctrl_ids`.
        Returns the 16-bit status code (DNR included)."""
        assert ctrl_ids, 'controller list must not be empty'
        self.q.mem_zero(BUF_ADDR, 4096)
        data = struct.pack('<' + 'H' * (len(ctrl_ids) + 1),
                           len(ctrl_ids), *ctrl_ids)
        self.q.mem_write(BUF_ADDR, data)
        _, sc = self.aq.run(build_ns_attachment(sel, nsid, BUF_ADDR))
        return sc

    def get_features(self, fid, cdw11=0):
        return self.aq.run(build_get_features(fid, cdw11))

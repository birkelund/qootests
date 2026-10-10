#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Shadow doorbell buffer (DBBUF) regression test (vnvme and nvme).

Covers: OACS.DBCS discovery (the controller must advertise shadow
doorbell buffer support or a real driver never uses it), Doorbell
Buffer Config validation (page alignment; non-mappable shadow buffer
addresses, e.g. the controller's own MMIO, are rejected without
enabling dbbuf), shadow
initialization for existing queues, I/O submission through the shadow
doorbells (the ring on the real doorbell is only a kick and carries a
wrong tail value), the event index announce path, admin queue shadow
sync on real doorbell writes, and that a controller reset clears the
negotiated dbbuf state (plain doorbell flow afterwards, then
re-establishing dbbuf).

Runs against both the vnvme and nvme devices, each with ioeventfd
off and on (with ioeventfd=on the doorbell writes are dispatched to
the event notifier handlers instead of the MMIO write handlers).
"""
import os
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nvme import (NvmeDev, make_disk, check, build_rw, build_get_features,
                  build_dbbuf_config, DATA_ADDR, DBS_ADDR, EIS_ADDR,
                  ID_CTRL_OACS, ID_NS_DS, NVME_OACS_DBCS, ST_SUCCESS,
                  ST_INVALID_FIELD, ST_DNR, FEAT_NUM_QUEUES, CMD_READ,
                  CMD_WRITE)

PAGE = 4096
NLB = 8                        # blocks per I/O


class DbbufQueue:
    """
    I/O queue pair driven like a dbbuf-aware driver: submission queue
    tails and completion queue heads are written to the shadow doorbell
    buffers; the real doorbell is only ever rung as a kick with a value
    that is NOT the tail, proving that the device takes the
    authoritative indices from the shadows.
    """

    def __init__(self, io, qid):
        self.io = io
        self.halves = PAGE >> 1
        # shadow doorbell buffer slots (CAP.DSTRD = 0 layout)
        self.sq_slot = DBS_ADDR + (qid << 3)
        self.cq_slot = DBS_ADDR + (qid << 3) + 4
        # shadow event index slots
        self.sq_eis = EIS_ADDR + (qid << 3)
        self.cq_eis = EIS_ADDR + (qid << 3) + 4

    def place(self, cmd):
        """Put a command in the SQ withOUT ringing any doorbell."""
        cmd = bytearray(cmd)
        cid = self.io.dev.next_cid()
        struct.pack_into('<H', cmd, 2, cid)
        assert self.io.sq_tail < self.io.qsize
        self.io.dev.q.mem_write(self.io.sq_addr + self.io.sq_tail * 64, cmd)
        self.io.sq_tail += 1
        self.io.pending.append(cid)
        return cid

    def kick(self, tail=None, wrong=0):
        """Advance the shadow tail and ring the real doorbell as a kick."""
        if tail is None:
            tail = self.io.sq_tail
        self.io.dev.q.writel(self.sq_slot, tail)
        # ring with a value that is NOT the tail; with DBBUF enabled the
        # device must re-read the tail from the shadow
        self.io.dev.q.writel(self.io.sq_db, wrong)

    def wait_cqe(self, timeout=5.0):
        """Consume the next CQE (any pending CID; async completions may
        overtake each other) and update the shadow cq head like a
        driver. Returns (cid, status)."""
        io = self.io
        slot = io.cq_addr + io.cq_head * 16
        deadline = time.time() + timeout
        while True:
            got_cid = io.dev.q.readw(slot + 12)
            if got_cid in io.pending:
                break
            if time.time() > deadline:
                raise TimeoutError(f'timed out waiting for CQE with cids '
                                   f'{io.pending} (got cid 0x{got_cid:x})')
            time.sleep(0.001)
        status = io.dev.q.readw(slot + 14)
        check(status & 1, io.phase, 'CQE phase bit')
        io.pending.remove(got_cid)
        io.cq_head = (io.cq_head + 1) % io.qsize
        if io.cq_head == 0:
            io.phase ^= 1
        io.dev.q.writel(io.cq_db, io.cq_head)
        io.dev.q.writel(self.cq_slot, io.cq_head)
        return got_cid, (status >> 1) & 0xffff


def run(tmp, device, ioeventfd=False):
    disk = make_disk(tmp)
    opts = 'ioeventfd=on' if ioeventfd else ''
    with NvmeDev(disk, workdir=tmp, device=device, device_opts=opts) as dev:
        dev.setup_pci()
        dev.enable()

        # discovery: a real driver only sets up the shadow doorbell
        # buffers when the controller advertises OACS.DBCS (e.g. Linux
        # bails out of dbbuf setup entirely without the bit, silently
        # paying an MMIO exit per submission doorbell). The command
        # working is not enough - the controller must be discovered.
        ctrl = dev.identify_ctrl()
        oacs = struct.unpack_from('<H', ctrl, ID_CTRL_OACS)[0]
        check(oacs & NVME_OACS_DBCS, NVME_OACS_DBCS, 'OACS.DBCS advertised')

        bs = 1 << dev.identify_ns(1)[ID_NS_DS]

        dev.q.mem_zero(DBS_ADDR, PAGE)
        dev.q.mem_zero(EIS_ADDR, PAGE)

        # Doorbell Buffer Config: addresses must be page aligned
        _, sc = dev.aq.run(build_dbbuf_config(DBS_ADDR + 4, EIS_ADDR))
        check(sc, ST_INVALID_FIELD | ST_DNR, 'dbbuf_config misaligned dbs')
        _, sc = dev.aq.run(build_dbbuf_config(DBS_ADDR, EIS_ADDR + 4))
        check(sc, ST_INVALID_FIELD | ST_DNR, 'dbbuf_config misaligned eis')

        # The shadow buffers must be mappable memory: the device
        # initializes the shadow doorbells and writes the event indexes
        # without bouncing. Pointing them at the controller's own MMIO
        # must be rejected instead of silently enabling dbbuf and
        # dropping every shadow update (wedging a polling host).
        dev.q.mem_zero(DBS_ADDR, PAGE)
        _, sc = dev.aq.run(build_dbbuf_config(dev.bar0, EIS_ADDR))
        check(sc, ST_INVALID_FIELD | ST_DNR, 'dbbuf_config dbs in own MMIO')
        _, sc = dev.aq.run(build_dbbuf_config(DBS_ADDR, dev.bar0))
        check(sc, ST_INVALID_FIELD | ST_DNR, 'dbbuf_config eis in own MMIO')

        # dbbuf must remain disabled: the plain admin doorbell writes
        # of the commands above must not have been mirrored into the
        # (still zeroed) shadow page
        _, sc = dev.aq.run(build_get_features(FEAT_NUM_QUEUES))
        check(sc, ST_SUCCESS, 'Get Features after rejected dbbuf_config')
        check(dev.q.readl(DBS_ADDR), 0, 'dbbuf not enabled after rejection')

        io = dev.create_io_queue(1)
        db = DbbufQueue(io, 1)

        # enable the shadow doorbell buffer
        admin_tail = dev.aq.sq_tail
        _, sc = dev.aq.run(build_dbbuf_config(DBS_ADDR, EIS_ADDR))
        check(sc, ST_SUCCESS, 'dbbuf_config')

        # the shadow entries of the existing queues must have been
        # initialized with their current indices
        check(dev.q.readl(db.sq_slot), io.sq_tail, 'shadow sq tail initialized')
        check(dev.q.readl(db.cq_slot), 0, 'shadow cq head initialized')
        check(dev.q.readl(DBS_ADDR), admin_tail + 1,
              'shadow admin sq initialized')

        # write a pattern and read it back -- two commands submitted,
        # shadow tail advanced to 2, real doorbell ringed with a wrong
        # value: the device must process exactly the shadow tail
        data = bytes((i * 13 + 0x3c) & 0xff for i in range(NLB * bs))
        dev.q.mem_write(DATA_ADDR, data)
        db.place(build_rw(CMD_WRITE, 1, 0x10, NLB, prp1=DATA_ADDR))

        # mismatching expected data: read over a fresh buffer
        dev.q.mem_zero(DATA_ADDR + 0x1000, len(data))
        db.place(build_rw(CMD_READ, 1, 0x10, NLB, prp1=DATA_ADDR + 0x1000))

        db.kick()      # shadow says 2, the doorbell ring says 0

        for _ in range(2):
            _, sc = db.wait_cqe()
            check(sc, ST_SUCCESS, 'I/O through shadow doorbell')

        # the read must have returned the written data (both commands
        # targeted the same LBAs, so a wrong doorbell tail - processing
        # none or both commands - would fail the CQE checks above)
        got = dev.q.mem_read(DATA_ADDR + 0x1000, len(data))
        if got != data:
            raise AssertionError('readback mismatch through shadow doorbell')

        # device must have announced the consumed tail in the event index
        check(dev.q.readl(db.sq_eis), 2, 'event index announce (sq tail)')

        # admin queue: a real doorbell write must keep the admin shadow
        # in sync (the device enforces this per the spec)
        _, sc = dev.aq.run(build_get_features(FEAT_NUM_QUEUES))
        check(sc, ST_SUCCESS, 'Get Features after dbbuf_config')
        check(dev.q.readl(DBS_ADDR), dev.aq.sq_tail,
              'admin sq shadow kept in sync on doorbell write')

        # controller reset must drop the negotiated dbbuf state
        dev.disable()
        dev.enable()
        dev.q.mem_zero(DBS_ADDR, PAGE)
        dev.q.mem_zero(EIS_ADDR, PAGE)

        io = dev.create_io_queue(1)
        # plain (non-shadow) doorbell flow must still work
        dev.q.mem_write(DATA_ADDR, data)
        _, sc = io.run(build_rw(CMD_WRITE, 1, 0x20, NLB, prp1=DATA_ADDR))
        check(sc, ST_SUCCESS, 'Write (plain doorbell after reset)')

        # and real doorbell writes must no longer sync the admin shadow
        _, sc = dev.aq.run(build_get_features(FEAT_NUM_QUEUES))
        check(sc, ST_SUCCESS, 'Get Features after reset')
        check(dev.q.readl(DBS_ADDR), 0, 'dbbuf state cleared on reset')

        # re-establish dbbuf on the existing queue and check the shadow
        # slots get re-initialized from the live indices
        _, sc = dev.aq.run(build_dbbuf_config(DBS_ADDR, EIS_ADDR))
        check(sc, ST_SUCCESS, 'dbbuf_config (re-establish)')
        check(dev.q.readl(DBS_ADDR + (1 << 3)),
              io.sq_tail, 'shadow re-initialized for existing queue')

        db = DbbufQueue(io, 1)
        dev.q.mem_zero(DATA_ADDR, len(data))
        db.place(build_rw(CMD_READ, 1, 0x20, NLB, prp1=DATA_ADDR))
        db.kick()
        _, sc = db.wait_cqe()
        check(sc, ST_SUCCESS, 'Read (re-established dbbuf)')

        dev.disable()


def main():
    for device in ('vnvme', 'nvme'):
        for ioeventfd in (False, True):
            tag = f'{device},ioeventfd={int(ioeventfd)}'
            with tempfile.TemporaryDirectory(prefix='qootest-') as tmp:
                run(tmp, device, ioeventfd)
            print(f'test_vnvme_dbbuf[{tag}]: PASS')


if __name__ == '__main__':
    main()

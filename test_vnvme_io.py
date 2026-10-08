#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
vnvme I/O-path regression test.

Covers: I/O queue creation/deletion, write/read data integrity, OOB
read rejection, Write Zeroes, Flush, DSM (deallocate), single-namespace
enforcement (the production device is hardcoded to NSID 1), negative
queue deletion, and that I/O queues can be re-created after a controller
reset.
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nvme import (NvmeDev, make_disk, check, build_rw, build_flush,
                  build_write_zeroes, build_dsm, build_delete_sq,
                  build_delete_cq, DATA_ADDR, ID_NS_NSZE, ID_NS_DS,
                  NSID_BROADCAST, ST_SUCCESS, ST_LBA_RANGE,
                  ST_INVALID_QID, ST_INVALID_CQID, ST_INVALID_NSID,
                  ST_DNR, CMD_WRITE, CMD_READ)

# guest buffer for the DSM range entry
DSM_BUF = DATA_ADDR + 0x1000


def ns_params(dev):
    ns = dev.identify_ns(1)
    nsze = struct.unpack_from('<Q', ns, ID_NS_NSZE)[0]
    ds = ns[ID_NS_DS]
    return nsze, 1 << ds


def io_write_read(io, nsid, slba, nlb, bs):
    """Write a pattern, read it back, verify contents. Returns data."""
    data = bytes((i * 7 + 0x5a) & 0xff for i in range(nlb * bs))
    dev = io.dev
    assert len(data) <= 4096
    dev.q.mem_write(DATA_ADDR, data)
    _, sc = io.run(build_rw(CMD_WRITE, nsid, slba, nlb, prp1=DATA_ADDR))
    check(sc, ST_SUCCESS, 'Write')

    dev.q.mem_zero(DATA_ADDR, 4096)
    _, sc = io.run(build_rw(CMD_READ, nsid, slba, nlb, prp1=DATA_ADDR))
    check(sc, ST_SUCCESS, 'Read')

    got = dev.q.mem_read(DATA_ADDR, len(data))
    if got != data:
        first = next((i, a, b) for i, (a, b) in enumerate(zip(data, got))
                     if a != b)
        raise AssertionError(f'readback mismatch at offset {first[0]}: '
                             f'want 0x{first[1]:02x}, got 0x{first[2]:02x}')


def run(tmp):
    disk = make_disk(tmp)
    with NvmeDev(disk, workdir=tmp) as dev:
        dev.setup_pci()
        dev.enable()

        nsze, bs = ns_params(dev)
        nlb = 4096 // bs

        # create I/O queue pair 1
        io = dev.create_io_queue(1)

        # write and read back a pattern
        io_write_read(io, 1, 0x10, nlb, bs)
        io_write_read(io, 1, 0x100, nlb, bs)

        # OOB read: last valid lba is nsze - 1; read past the end
        _, sc = io.run(build_rw(CMD_READ, 1, nsze - nlb, nlb + 1,
                                prp1=DATA_ADDR))
        check(sc, ST_LBA_RANGE | ST_DNR, 'OOB read (end of namespace)')

        # Write Zeroes over the first-written area, read back, verify zero
        _, sc = io.run(build_write_zeroes(1, 0x10, nlb))
        check(sc, ST_SUCCESS, 'Write Zeroes')
        dev.q.mem_write(DATA_ADDR, b'\xa5' * 4096)
        _, sc = io.run(build_rw(CMD_READ, 1, 0x10, nlb, prp1=DATA_ADDR))
        check(sc, ST_SUCCESS, 'Read after Write Zeroes')
        got = dev.q.mem_read(DATA_ADDR, nlb * bs)
        if any(got):
            raise AssertionError('Write Zeroes did not zero the blocks')

        # Flush
        _, sc = io.run(build_flush(1))
        check(sc, ST_SUCCESS, 'Flush')

        # DSM (deallocate)
        dev.q.mem_write(DSM_BUF, struct.pack('<QII', 0x10, nlb, 0))
        _, sc = io.run(build_dsm(1, DSM_BUF, 1))
        check(sc, ST_SUCCESS, 'DSM deallocate')
        # dealloc'd blocks must read back without error (content may be
        # stale on a raw file, so only check the command)
        _, sc = io.run(build_rw(CMD_READ, 1, 0x10, nlb, prp1=DATA_ADDR))
        check(sc, ST_SUCCESS, 'Read after DSM')

        # single-namespace enforcement: the production device is
        # hardcoded to NSID 1 -- other namespaces do not exist and are
        # rejected with Invalid Namespace Or Format. A broadcast
        # (0xffffffff) flush targets 'all namespaces' and succeeds on
        # the one namespace.
        _, sc = io.run(build_rw(CMD_READ, 0, 0, nlb, prp1=DATA_ADDR))
        check(sc, ST_INVALID_NSID | ST_DNR, 'Read nsid 0')
        _, sc = io.run(build_rw(CMD_WRITE, 2, 0, nlb, prp1=DATA_ADDR))
        check(sc, ST_INVALID_NSID | ST_DNR, 'Write nsid 2')
        _, sc = io.run(build_rw(CMD_READ, NSID_BROADCAST, 0, nlb,
                                prp1=DATA_ADDR))
        check(sc, ST_INVALID_NSID | ST_DNR, 'Read broadcast nsid')
        _, sc = io.run(build_flush(NSID_BROADCAST))
        check(sc, ST_SUCCESS, 'Flush all namespaces (broadcast)')
        _, sc = io.run(build_flush(2))
        check(sc, ST_INVALID_NSID | ST_DNR, 'Flush nsid 2')

        # queue teardown and negative cases: deleting an SQ that does not
        # exist must fail; same for the CQ. delete SQ 1 twice -> the
        # second must fail
        _, sc = dev.aq.run(build_delete_sq(1))
        check(sc, ST_SUCCESS, 'Delete SQ 1')
        _, sc = dev.aq.run(build_delete_sq(1))
        check(sc, ST_INVALID_QID | ST_DNR, 'Delete SQ 1 again')

        _, sc = dev.aq.run(build_delete_cq(1))
        check(sc, ST_SUCCESS, 'Delete CQ 1')
        _, sc = dev.aq.run(build_delete_cq(1))
        check(sc, ST_INVALID_CQID | ST_DNR, 'Delete CQ 1 again')

        # controller reset, then create the queue pair again over the
        # same guest memory (stale CQEs must not confuse completions)
        dev.disable()
        dev.enable()
        io = dev.create_io_queue(1)
        io_write_read(io, 1, 0x200, nlb, bs)
        dev.delete_io_queue(1)

        dev.disable()


def main():
    with tempfile.TemporaryDirectory(prefix='qootest-') as tmp:
        run(tmp)
    print('test_vnvme_io: PASS')


if __name__ == '__main__':
    main()

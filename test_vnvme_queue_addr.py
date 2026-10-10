#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
I/O queue placement semantics (vnvme and nvme).

Covers where Create I/O Submission/Completion Queue may place the queue
memory, and what happens when it does not:

  - Own MMIO: a queue inside the controller's own register file is
    rejected up front with Invalid Field for both devices; without the
    check the first command fetch / completion posting would hit the
    re-entrancy guard and wedge the controller in CSTS.FAILED.
  - Foreign MMIO: a queue inside the register file of a *second* NVMe
    controller. The production device requires queue memory to be
    directly mappable (it is accessed on the submission/completion
    paths without bouncing) and rejects the creation; the test device
    keeps the permissive semantics (mirroring its dptr bounce policy)
    and accepts it - the queue is never driven, just deleted again.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nvme import (NvmeDev, make_disk, check, build_create_cq, build_create_sq,
                  build_delete_sq, build_delete_cq, build_rw, CMD_READ,
                  DATA_ADDR, ID_NS_DS, IO_CQ_ADDR, ST_SUCCESS,
                  ST_INVALID_FIELD, ST_DNR)

# second controller at PCI 05.0, feeding MMIO as the queue target
DEV2_CFG = 0x80000000 | (5 << 11)
DEV2_BAR0 = 0xfe300000        # outside dev1's own BAR0 span (BAR mbar)
DEV2_SQ = DEV2_BAR0 + 0x1000  # distinct page for the SQ

QSIZE = 16


def map_feed_dev(dev):
    """Enable memory decode on the second controller and map its BAR0."""
    dev.q.pci_cfg_write32(DEV2_CFG + 0x04, 0x02)   # command: MEM space
    dev.q.pci_cfg_write32(DEV2_CFG + 0x10, DEV2_BAR0)
    dev.q.pci_cfg_write32(DEV2_CFG + 0x14, 0)      # 64-bit BAR: high word


def queue_addr(tmp, device):
    os.makedirs(os.path.join(tmp, 'qaddr'), exist_ok=True)
    os.makedirs(os.path.join(tmp, 'qaddr-feed'), exist_ok=True)
    disk = make_disk(os.path.join(tmp, 'qaddr'))
    feed = make_disk(os.path.join(tmp, 'qaddr-feed'))

    pre = ['-drive', f'if=none,id=feed0,file={feed},format=raw',
           '-device', f'{device},drive=feed0,serial=feed,addr=05.0']

    with NvmeDev(disk, workdir=os.path.join(tmp, 'qaddr'), device=device,
                 pre_args=pre) as dev:
        dev.setup_pci()
        map_feed_dev(dev)
        dev.enable()

        # source of truth: the second controller's registers as the
        # guest itself would read them
        check(dev.q.mem_read(DEV2_BAR0, 16) != b'', True,
              'feed controller BAR0 readable')

        # own MMIO: rejected up front for both devices
        _, sc = dev.aq.run(build_create_cq(1, dev.bar0, QSIZE))
        check(sc, ST_INVALID_FIELD | ST_DNR, 'own MMIO CQ rejected')
        _, sc = dev.aq.run(build_create_cq(1, IO_CQ_ADDR, QSIZE))
        check(sc, ST_SUCCESS, 'Create CQ 1 (RAM)')
        _, sc = dev.aq.run(build_create_sq(1, 1, dev.bar0, QSIZE))
        check(sc, ST_INVALID_FIELD | ST_DNR, 'own MMIO SQ rejected')

        # foreign MMIO
        _, sc = dev.aq.run(build_create_cq(2, DEV2_BAR0, QSIZE))
        if device == 'vnvme':
            # hardened: queue memory must be mappable
            check(sc, ST_INVALID_FIELD | ST_DNR,
                  'vnvme: foreign MMIO CQ rejected')
        else:
            # permissive: accepted (never driven), then deleted
            check(sc, ST_SUCCESS, 'nvme: foreign MMIO CQ accepted')

        _, sc = dev.aq.run(build_create_sq(2, 1, DEV2_SQ, QSIZE))
        if device == 'vnvme':
            check(sc, ST_INVALID_FIELD | ST_DNR,
                  'vnvme: foreign MMIO SQ rejected')
            # the rejected creations must not have left queue state
            # behind
            _, sc = dev.aq.run(build_delete_cq(2))
            check(sc != ST_SUCCESS, True,
                  'vnvme: no CQ 2 left behind by rejected creation')
        else:
            check(sc, ST_SUCCESS, 'nvme: foreign MMIO SQ accepted')
            _, sc = dev.aq.run(build_delete_sq(2))
            check(sc, ST_SUCCESS, 'Delete SQ 2')
            _, sc = dev.aq.run(build_delete_cq(2))
            check(sc, ST_SUCCESS, 'Delete CQ 2')

        # a valid queue in RAM still works end to end
        io = dev.create_io_queue(3)
        bs = 1 << dev.identify_ns(1)[ID_NS_DS]
        dev.q.mem_zero(DATA_ADDR, 4096)
        _, sc = io.run(build_rw(CMD_READ, 1, 0, 4096 // bs, prp1=DATA_ADDR))
        check(sc, ST_SUCCESS, 'I/O on RAM queue')
        dev.delete_io_queue(3)


def main():
    for device in ('vnvme', 'nvme'):
        with tempfile.TemporaryDirectory(prefix='qootest-') as tmp:
            queue_addr(tmp, device)
        print(f'test_vnvme_queue_addr[{device}]: PASS')
    print('test_vnvme_queue_addr: PASS')


if __name__ == '__main__':
    main()

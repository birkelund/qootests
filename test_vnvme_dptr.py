#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Data-pointer placement semantics (vnvme and nvme).

Covers where command data pointers (DPTR) may live, and what happens
when they do not:

  - MMIO data pointers: a buffer pointing at the register file of a
    *second* NVMe controller — memory that cannot be mapped into host
    RAM. The production device rejects the command with a Data Transfer
    Error (lines of defense: no bouncing of device-internal or foreign
    MMIO onto the data path); the test device bounces it and completes
    successfully.
  - PMR data pointers (nvme only; vnvme has no PMR): a buffer living in
    the persistent memory region goes through the indirect RAM mapping
    path, with a write-from-PMR / read-into-PMR data integrity
    roundtrip.
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nvme import (NvmeDev, make_disk, check, build_rw, PCI_CFG,
                  REG_PMRCTL, ST_SUCCESS, ST_DATA_TRAS_ERROR, ST_DNR,
                  DATA_ADDR, ID_NS_NSZE, ID_NS_DS, CMD_READ, CMD_WRITE)

# second controller at PCI 05.0, feeding MMIO as the dptr target
DEV2_CFG = 0x80000000 | (5 << 11)
DEV2_BAR0 = 0xfe300000        # outside dev1's own BAR0 span (BAR mbar)

# PMR BAR4 (64-bit prefetchable), nvme test device only
PMR_BAR = 0xfe600000
PMR_DATA = PMR_BAR + 0x2000

PMR_SIZE = 1 << 20


def ns_params(dev):
    ns = dev.identify_ns(1)
    nsze = struct.unpack_from('<Q', ns, ID_NS_NSZE)[0]
    ds = ns[ID_NS_DS]
    return nsze, 1 << ds


def map_feed_dev(dev):
    """Enable memory decode on the second controller and map its BAR0."""
    dev.q.pci_cfg_write32(DEV2_CFG + 0x04, 0x02)   # command: MEM space
    dev.q.pci_cfg_write32(DEV2_CFG + 0x10, DEV2_BAR0)
    dev.q.pci_cfg_write32(DEV2_CFG + 0x14, 0)      # 64-bit BAR: high word


def mmio_dptr(tmp, device):
    """READ with the data buffer inside another device's MMIO."""
    os.makedirs(os.path.join(tmp, 'mmio'), exist_ok=True)
    os.makedirs(os.path.join(tmp, 'mmio-feed'), exist_ok=True)
    disk = make_disk(os.path.join(tmp, 'mmio'))
    feed = make_disk(os.path.join(tmp, 'mmio-feed'))

    pre = ['-drive', f'if=none,id=feed0,file={feed},format=raw',
           '-device', f'{device},drive=feed0,serial=feed,addr=05.0']

    with NvmeDev(disk, workdir=os.path.join(tmp, 'mmio'), device=device,
                 pre_args=pre) as dev:
        dev.setup_pci()
        map_feed_dev(dev)
        dev.enable()

        _, bs = ns_params(dev)
        nlb = 4096 // bs
        io = dev.create_io_queue(1)

        # source of truth: the second controller's registers as the
        # guest itself would read them
        check(dev.q.mem_read(DEV2_BAR0, 16) != b'', True,
              'feed controller BAR0 readable')

        _, sc = io.run(build_rw(CMD_READ, 1, 0x300, nlb, prp1=DEV2_BAR0))
        if device == 'vnvme':
            # hardened: non-mappable memory is rejected outright
            # (DNR is OR'ed in by the read command's error path)
            check(sc, ST_DATA_TRAS_ERROR | ST_DNR,
                  'vnvme: MMIO dptr rejected')
        else:
            # the test device bounces non-mappable memory
            check(sc, ST_SUCCESS, 'nvme: MMIO dptr bounced')


def pmr_dptr(tmp):
    """Data buffers inside the PMR (nvme test device only)."""
    os.makedirs(os.path.join(tmp, 'pmr'), exist_ok=True)
    disk = make_disk(os.path.join(tmp, 'pmr'))

    pre = ['-object', f'memory-backend-ram,id=pmr0,share=on,size={PMR_SIZE}']

    with NvmeDev(disk, workdir=os.path.join(tmp, 'pmr'), device='nvme',
                 pre_args=pre, device_opts='pmrdev=pmr0') as dev:
        dev.setup_pci()

        # map the 64-bit PMR BAR4 and enable the PMR
        dev.q.pci_cfg_write32(PCI_CFG + 0x20, PMR_BAR)
        dev.q.pci_cfg_write32(PCI_CFG + 0x24, 0)

        dev.enable()

        dev.reg_write(REG_PMRCTL, 1)
        check(dev.q.readl(PMR_DATA) == 0, True, 'PMR accessible')

        _, bs = ns_params(dev)
        nlb = 4096 // bs
        io = dev.create_io_queue(1)

        data = bytes((i * 13 + 0x77) & 0xff for i in range(nlb * bs))

        # guest (qtest) writes a pattern into the PMR; the device DMA
        # reads it to disk
        dev.q.mem_write(PMR_DATA, data)
        _, sc = io.run(build_rw(CMD_WRITE, 1, 0x400, nlb, prp1=PMR_DATA))
        check(sc, ST_SUCCESS, 'Write from PMR')

        # zero the PMR buffer, read the block back into it
        dev.q.mem_zero(PMR_DATA, 4096)
        _, sc = io.run(build_rw(CMD_READ, 1, 0x400, nlb, prp1=PMR_DATA))
        check(sc, ST_SUCCESS, 'Read into PMR')
        check(dev.q.mem_read(PMR_DATA, len(data)) == data, True,
              'PMR readback matches')


def run(tmp):
    for device in ('vnvme', 'nvme'):
        mmio_dptr(tmp, device)
        print(f'test_vnvme_dptr[{device},mmio]: PASS')
    pmr_dptr(tmp)
    print('test_vnvme_dptr[nvme,pmr]: PASS')


def main():
    with tempfile.TemporaryDirectory(prefix='qootest-') as tmp:
        run(tmp)
    print('test_vnvme_dptr: PASS')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
vnvme CMB regression test (test device only; vnvme has no CMB).

Covers both CMB schemes against the RAM-backed CMB memory region:
  - 'legacy-cmb=on': CMBSZ/CMBLOC pre-enabled, CMB lives at the BAR.
  - v1.4 scheme: the guest enables the CMB via CMBMSC (CRE|CMSE|CBA).

For each scheme: guest (qtest) access to the CMB BAR as plain memory,
I/O with data buffers in the CMB (write from CMB, read into CMB), and
an I/O submission queue living in the CMB. The v1.4 leg also checks the
CRE=0 teardown (CMBSZ/CMBLOC cleared).
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nvme import (NvmeDev, make_disk, check, build_rw, PCI_CFG_BAR2,
                  REG_CAP, REG_CMBSZ, REG_CMBMSC, CMD_WRITE, CMD_READ,
                  ST_SUCCESS, DATA_ADDR, ID_NS_NSZE, ID_NS_DS)

BAR2 = 0xfe400000              # CMB BAR (64-bit, prefetchable RAM)
CMB_DATA = BAR2 + 0x2000       # I/O data buffer inside the CMB
CMB_SQ = BAR2 + 0x10000        # submission queue inside the CMB
CQ2_ADDR = 0x610000            # second I/O completion queue (in RAM)

CMBMSC_CRE = 1 << 0
CMBMSC_CMSE = 1 << 1

CAP_CMBS_SHIFT = 3


def ns_params(dev):
    ns = dev.identify_ns(1)
    nsze = struct.unpack_from('<Q', ns, ID_NS_NSZE)[0]
    ds = ns[ID_NS_DS]
    return nsze, 1 << ds


def map_cmb_bar(dev):
    dev.q.pci_cfg_write32(PCI_CFG_BAR2 + 4, 0)
    dev.q.pci_cfg_write32(PCI_CFG_BAR2, BAR2)


def cmb_rw_roundtrip(io, dev, bs, tag):
    """Data out of and into the CMB, verified from both ends."""
    nlb = 4096 // bs
    data = bytes((i * 11 + 0x3c) & 0xff for i in range(nlb * bs))

    # guest writes a pattern into the CMB; the device writes it to disk
    dev.q.mem_write(CMB_DATA, data)
    _, sc = io.run(build_rw(CMD_WRITE, 1, 0x100, nlb, prp1=CMB_DATA))
    check(sc, ST_SUCCESS, f'{tag}: Write from CMB')

    # read it back into guest RAM and compare
    dev.q.mem_zero(DATA_ADDR, 4096)
    _, sc = io.run(build_rw(CMD_READ, 1, 0x100, nlb, prp1=DATA_ADDR))
    check(sc, ST_SUCCESS, f'{tag}: Read into RAM')
    check(dev.q.mem_read(DATA_ADDR, len(data)) == data, True,
          f'{tag}: RAM readback matches CMB-written data')

    # zero the CMB buffer, read the block back into the CMB, compare
    dev.q.mem_zero(CMB_DATA, 4096)
    _, sc = io.run(build_rw(CMD_READ, 1, 0x100, nlb, prp1=CMB_DATA))
    check(sc, ST_SUCCESS, f'{tag}: Read into CMB')
    check(dev.q.mem_read(CMB_DATA, len(data)) == data, True,
          f'{tag}: CMB readback matches')


def run_scheme(tmp, legacy):
    opts = 'cmb_size_mb=2'
    if legacy:
        opts += ',legacy-cmb=on'
    tag = 'legacy-cmb' if legacy else 'v1.4'

    os.makedirs(tmp, exist_ok=True)
    disk = make_disk(tmp)
    with NvmeDev(disk, workdir=tmp, device='nvme', device_opts=opts) as dev:
        dev.setup_pci()
        map_cmb_bar(dev)

        # CAP.CMBS must be advertised
        cap = dev.q.readl(dev.bar0 + REG_CAP)
        check((cap >> CAP_CMBS_SHIFT) & 1, 1, f'{tag}: CAP.CMBS')

        if not legacy:
            # v1.4: enable via CMBMSC. Write the high word first; the
            # enable logic runs when the low word lands.
            dev.reg_write(REG_CMBMSC + 4, 0)
            dev.reg_write(REG_CMBMSC, CMBMSC_CRE | CMBMSC_CMSE | BAR2)

        check(dev.reg_read(REG_CMBSZ) != 0, True, f'{tag}: CMBSZ set')

        dev.enable()
        _, bs = ns_params(dev)

        # I/O queue pair 1 (CQ in RAM), data roundtrip through the CMB
        io = dev.create_io_queue(1)
        cmb_rw_roundtrip(io, dev, bs, tag)

        # a submission queue living in the CMB
        sq_in_cmb = dev.create_io_queue(2, sq_addr=CMB_SQ, cq_addr=CQ2_ADDR)
        nlb = 4096 // bs
        data = bytes((i * 5 + 0xa1) & 0xff for i in range(4096))
        dev.q.mem_write(DATA_ADDR, data)
        _, sc = sq_in_cmb.run(build_rw(CMD_WRITE, 1, 0x200, nlb,
                                       prp1=DATA_ADDR))
        check(sc, ST_SUCCESS, f'{tag}: Write via CMB-resident SQ')
        dev.q.mem_zero(DATA_ADDR, 4096)
        _, sc = sq_in_cmb.run(build_rw(CMD_READ, 1, 0x200, nlb,
                                       prp1=DATA_ADDR))
        check(sc, ST_SUCCESS, f'{tag}: Read via CMB-resident SQ')
        check(dev.q.mem_read(DATA_ADDR, 4096) == data, True,
              f'{tag}: CMB-resident SQ data integrity')

        if not legacy:
            # CRE=0 tears down: CMBSZ/CMBLOC are cleared
            dev.reg_write(REG_CMBMSC, 0)
            check(dev.reg_read(REG_CMBSZ), 0,
                  f'{tag}: CMBSZ cleared on CRE=0')


def run(tmp):
    run_scheme(os.path.join(tmp, 'legacy'), legacy=True)
    run_scheme(os.path.join(tmp, 'v14'), legacy=False)


def main():
    with tempfile.TemporaryDirectory(prefix='nvme-cmb-') as tmp:
        run(tmp)
    print('test_vnvme_cmb: PASS')


if __name__ == '__main__':
    main()

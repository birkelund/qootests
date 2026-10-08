#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
vnvme admin-path regression test.

Covers: PCI BAR setup, controller enable/disable transitions, Identify
Controller/Namespace, Get Features (supported and unsupported FIDs),
invalid admin opcode handling, and that the admin queue survives a
controller reset (CC.EN=0 -> 1 re-enable).
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nvme import (NvmeDev, make_disk, check, ID_CTRL_VID, ID_CTRL_OACS,
                  ID_CTRL_NN, ID_CTRL_ONCS, ID_CTRL_SQES, ID_NS_NSZE,
                  ID_NS_DS, NVME_OACS_FORMAT, ADM_FORMAT, REG_CAP, REG_ASQ,
                  REG_ACQ, BUF_ADDR, FEAT_NUM_QUEUES, build_identify,
                  CNS_ACTIVE_LIST, CNS_DESCR_LIST, ST_SUCCESS,
                  ST_INVALID_FIELD, ST_INVALID_NSID, ST_INVALID_OPCODE,
                  ST_DNR)


def run(tmp):
    disk = make_disk(tmp)
    with NvmeDev(disk, workdir=tmp) as dev:
        dev.setup_pci()

        # controller must not be ready before we enable it
        check(dev.is_rdy(), 0, 'RDY before enable')

        aq = dev.enable()

        # CAP.MQES must be 0-based and at least our queue size - 1 (CAP is
        # 64 bits but readl of the low half is enough for the bits we want)
        cap = dev.reg_read(REG_CAP)
        mqes = cap & 0xffff
        if mqes < 15:
            raise AssertionError(f'CAP.MQES too small: {mqes}')

        # Identify Controller
        ctrl = dev.identify_ctrl()
        vid = struct.unpack_from('<H', ctrl, ID_CTRL_VID)[0]
        nn = struct.unpack_from('<I', ctrl, ID_CTRL_NN)[0]
        oncs = struct.unpack_from('<H', ctrl, ID_CTRL_ONCS)[0]
        sqes = ctrl[ID_CTRL_SQES]
        check(vid, 0x1b36, 'Identify Controller vendor id')
        check(nn, 1, 'Identify Controller number of namespaces')
        check(oncs, 0x000c, 'Identify Controller ONCS (write zeroes + dsm)')
        check(sqes, 0x66, 'Identify Controller SQES (min 6, max 6)')

        # vnvme must not support Format NVM: neither advertise it...
        oacs = struct.unpack_from('<H', ctrl, ID_CTRL_OACS)[0]
        check(oacs & NVME_OACS_FORMAT, 0, 'OACS must not advertise Format NVM')

        # ... nor accept the command
        c = bytearray(64)
        c[0] = ADM_FORMAT
        struct.pack_into('<I', c, 4, 1)      # nsid 1
        _, sc = aq.run(c)
        check(sc, ST_INVALID_OPCODE | ST_DNR, 'Format NVM is not supported')

        # Identify Namespace 1
        ns = dev.identify_ns(1)
        nsze = struct.unpack_from('<Q', ns, ID_NS_NSZE)[0]
        ds = ns[ID_NS_DS]
        if nsze == 0:
            raise AssertionError('Identify Namespace: NSZE is 0')
        check(ds, 9, 'Identify Namespace lba data size exponent (512B)')
        print(f'namespace 1: {nsze} blocks of {1 << ds} bytes')

        # Identify: Active Namespace List (CNS 0x02). The only namespace
        # (id 1) must be listed for min_nsid 0, the rest must be zeros.
        lst = dev.identify_ns_list()
        nsids = struct.unpack_from('<1024I', lst)
        check(nsids[0], 1, 'Active Namespace List lists nsid 1')
        if any(nsids[1:]):
            raise AssertionError('Active Namespace List has unexpected nsids')

        # The list holds namespaces with ids *higher* than the requested
        # nsid, so asking past nsid 1 must yield an empty list.
        lst = dev.identify_ns_list(min_nsid=1)
        if any(struct.unpack_from('<1024I', lst)):
            raise AssertionError('Active Namespace List past nsid 1 not empty')

        # A min_nsid of broadcast - 1 or higher is invalid.
        _, sc = dev.aq.run(build_identify(CNS_ACTIVE_LIST,
                                          nsid=0xfffffffe, prp1=BUF_ADDR))
        check(sc, ST_INVALID_NSID | ST_DNR,
              'Active Namespace List bad min_nsid (broadcast - 1)')

        # Identify: Namespace Identification Descriptor List (CNS 0x03).
        # vnvme exposes no namespace identifiers, so the returned list
        # must be empty.
        descr = dev.identify_ns_descr_list(nsid=1)
        if any(descr):
            raise AssertionError('Namespace Descriptor List must be empty')

        # An invalid nsid must be rejected.
        _, sc = dev.aq.run(build_identify(CNS_DESCR_LIST, nsid=0xdead,
                                         prp1=BUF_ADDR))
        check(sc, ST_INVALID_FIELD | ST_DNR, 'Namespace Descriptor List bad nsid')

        # Get Features: Number of Queues (0-based pair count)
        result, sc = dev.get_features(FEAT_NUM_QUEUES)
        check(sc, ST_SUCCESS, 'Get Features (Number of Queues)')
        check(result, 63 | (63 << 16), 'Get Features Number of Queues')

        # Get Features: unsupported FID must fail with DNR
        _, sc = dev.get_features(0x21)
        check(sc, ST_INVALID_FIELD | ST_DNR, 'Get Features unsupported FID')

        # unsupported admin opcode must fail with DNR
        c = bytearray(64)
        c[0] = 0x7f
        _, sc = aq.run(c)
        check(sc, ST_INVALID_OPCODE | ST_DNR, 'admin opcode 0x7f')

        # controller reset: disable, then re-enable. the admin queue must
        # be fully re-created (fresh state), so run an identify over it
        # afterwards.
        dev.disable()
        check(dev.is_rdy(), 0, 'RDY after disable')
        aq = dev.enable()
        check(dev.is_rdy(), 1, 'RDY after re-enable')

        ctrl = dev.identify_ctrl()
        vid = struct.unpack_from('<H', ctrl, ID_CTRL_VID)[0]
        check(vid, 0x1b36, 'Identify Controller vendor id after reset')

        # The ASQ and ACQ registers are 64-bit fields and a guest may
        # program them with separate 32-bit accesses (Linux does this on
        # powerpc, where the high word carries the DMA window offset).
        # The high-word accesses (ASQ + 4 / ACQ + 4) must be stored, and a
        # low-word rewrite must not clobber the high word.
        dev.disable()
        check(dev.is_rdy(), 0, 'RDY before split-write queue setup')

        asq2, acq2 = 0x800000, 0x810000

        dev.reg_write(REG_ASQ, asq2)
        dev.reg_write(REG_ASQ + 4, 0x2)
        check(dev.reg_read(REG_ASQ), asq2, 'ASQ low word')
        check(dev.reg_read(REG_ASQ + 4), 0x2, 'ASQ high word (split write)')

        dev.reg_write(REG_ASQ, asq2 + 0x1000)
        check(dev.reg_read(REG_ASQ + 4), 0x2,
              'ASQ low-word rewrite preserves high word')
        dev.reg_write(REG_ASQ + 4, 0)

        dev.reg_write(REG_ACQ, acq2)
        dev.reg_write(REG_ACQ + 4, 0x1)
        check(dev.reg_read(REG_ACQ), acq2, 'ACQ low word')
        check(dev.reg_read(REG_ACQ + 4), 0x1, 'ACQ high word (split write)')
        dev.reg_write(REG_ACQ, acq2 + 0x1000)
        check(dev.reg_read(REG_ACQ + 4), 0x1,
              'ACQ low-word rewrite preserves high word')
        dev.reg_write(REG_ACQ + 4, 0)

        # Admin queues at the split-programmed addresses must work
        # end to end.
        aq = dev.enable(asq=asq2 + 0x1000, acq=acq2 + 0x1000)
        check(dev.is_rdy(), 1, 'RDY after split-write enable')
        ctrl = dev.identify_ctrl()
        vid = struct.unpack_from('<H', ctrl, ID_CTRL_VID)[0]
        check(vid, 0x1b36, 'Identify Controller over split-programmed queues')

        # final disable
        dev.disable()
        check(dev.is_rdy(), 0, 'RDY after final disable')


def main():
    with tempfile.TemporaryDirectory(prefix='qootest-') as tmp:
        run(tmp)
    print('test_vnvme_admin: PASS')


if __name__ == '__main__':
    main()

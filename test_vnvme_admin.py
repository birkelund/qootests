#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
vnvme admin-path regression test.

Covers: PCI BAR setup, controller enable/disable transitions, Identify
Controller/Namespace, Get Features (supported and unsupported FIDs),
invalid admin opcode handling, and that the admin queue survives a
controller reset (CC.EN=0 -> 1 re-enable). The 64-bit admin queue
address registers are programmed both with split 32-bit accesses
(low/high word) and with single 64-bit (native width) writes. Per the
spec the host shall access registers in their native width or in
aligned 32-bit accesses; accesses targeting a portion of two or more
registers (an 8-byte write at VS, INTMC, or a doorbell) are not
supported and vnvme ignores them - that policy is pinned here.
"""
import os
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from uuid import uuid5

from nvme import (NvmeDev, NvmeQueue, make_disk, check, ID_CTRL_VID,
                  ID_CTRL_OACS, ID_CTRL_AERL, ID_CTRL_MDTS, ID_CTRL_NN,
                  ID_CTRL_ONCS, ID_CTRL_SQES, ID_NS_NSZE, ID_NS_DS,
                  NVME_OACS_FORMAT, ADM_FORMAT, REG_CAP, REG_ASQ, REG_ACQ,
                  BUF_ADDR, DB_BASE, FEAT_NUM_QUEUES, build_identify,
                  build_get_features, CNS_ACTIVE_LIST, CNS_DESCR_LIST,
                  NVME_NS_UUID_NAMESPACE, NIDT_UUID, NIDT_CSI, REG_VS,
                  REG_INTMS, REG_INTMC, REG_CC, REG_AQA, CC_EN, CC_IOSQES,
                  CC_IOCQES, ST_SUCCESS, ST_INVALID_FIELD, ST_INVALID_NSID,
                  ST_INVALID_OPCODE, ST_DNR)


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

        # AERL is a fixed device constant (vnvme never queues async
        # events, so there is nothing to tune); 0-based, so 3 admits
        # four outstanding Asynchronous Event Requests.
        check(ctrl[ID_CTRL_AERL], 3, 'Identify Controller AERL (fixed)')

        # MDTS is fixed at 9: the maximum transfer is 2 MiB (reported
        # in units of the 4 KiB emulated page size), the largest
        # fully-scattered PRP transfer that stays within the device
        # mapping path's iovec budget.
        check(ctrl[ID_CTRL_MDTS], 9, 'Identify Controller MDTS (fixed)')

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
        # vnvme exposes no configurable namespace identifiers; the UUID
        # is derived from the controller serial and the nsid as an
        # RFC 4122 version 5 UUID (see hw/nvme/core/ns.c). Verify the
        # descriptor list carries the derived UUID and the CSI.
        want_uuid = uuid5(NVME_NS_UUID_NAMESPACE, f'{dev.serial}:{1}')

        descr = dev.identify_ns_descr_list(nsid=1)
        entries = {}
        off = 0
        while off + 4 <= len(descr) and descr[off]:
            nidt, nidl = descr[off], descr[off + 1]
            entries[nidt] = bytes(descr[off + 4:off + 4 + nidl])
            off += 4 + nidl

        for nidt in (NIDT_UUID, NIDT_CSI):
            if nidt not in entries:
                raise AssertionError(
                    f'Namespace Descriptor List misses NIDT {nidt:#x}')
        check(entries[NIDT_UUID], want_uuid.bytes,
              'Namespace Descriptor List UUID (derived v5)')
        check(entries[NIDT_CSI][0], 0, 'Namespace Descriptor List CSI (NVM)')

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

        # Per the spec, the host shall access registers in their native
        # width or in aligned 32-bit accesses; accesses that target a
        # portion of two or more registers are not supported (undefined
        # device-side). vnvme ignores such writes and logs a guest
        # error; only the 64-bit ASQ/ACQ registers accept a 64-bit
        # write. That policy is pinned here.
        dev.disable()
        check(dev.is_rdy(), 0, 'RDY before 8-byte queue setup')

        asq3, acq3 = 0x820000, 0x830000

        # the 64-bit ASQ/ACQ registers accept single 64-bit (native
        # width) writes
        dev.reg_write(REG_AQA, 0xf000f)
        dev.q.writeq(dev.bar0 + REG_ASQ, asq3)
        dev.q.writeq(dev.bar0 + REG_ACQ, acq3)
        check(dev.reg_read(REG_ASQ), asq3, 'ASQ 8-byte write')
        check(dev.reg_read(REG_ACQ), acq3, 'ACQ 8-byte write')

        # an 8-byte write at VS spans VS (read-only) and INTMS; it must
        # be ignored in its entirety - the INTMS half must not apply
        dev.q.writeq(dev.bar0 + REG_VS, 0xff00 << 32)
        check(dev.reg_read(REG_INTMS), 0, 'VS+INTMS 8-byte write ignored')

        # an 8-byte write spanning INTMC and CC must be ignored in its
        # entirety: no interrupt unmask, and the controller must not
        # be enabled by the CC half
        dev.q.writeq(dev.bar0 + REG_INTMC,
                     ((CC_EN | CC_IOSQES | CC_IOCQES) << 32) | 0x0f00)
        check(dev.reg_read(REG_INTMS), 0, 'INTMC+CC 8-byte write ignored')
        check(dev.is_rdy(), 0, 'CC half of INTMC+CC 8-byte write ignored')

        # the doorbells are 32-bit registers; an 8-byte doorbell write
        # spans two doorbells and must be ignored entirely. place a
        # command and ring the admin SQ doorbell with a 64-bit write:
        # nothing must happen.
        dev.q.mem_zero(asq3, 16 * 64)
        dev.q.mem_zero(acq3, 16 * 16)
        dev.reg_write(REG_CC, CC_EN | CC_IOSQES | CC_IOCQES)
        dev._poll_rdy(1, 5.0)
        dev.aq = NvmeQueue(dev, asq3, acq3,
                           dev.bar0 + DB_BASE, dev.bar0 + DB_BASE + 4, 16)

        c = build_get_features(FEAT_NUM_QUEUES)
        cid = dev.next_cid()
        struct.pack_into('<H', c, 2, cid)
        dev.aq.pending.append(cid)
        dev.q.mem_write(dev.aq.sq_addr + dev.aq.sq_tail * 64, bytes(c))
        dev.aq.sq_tail += 1
        dev.q.writeq(dev.aq.sq_db, dev.aq.sq_tail)

        slot = dev.aq.cq_addr + dev.aq.cq_head * 16
        deadline = time.time() + 0.5
        while time.time() < deadline:
            if dev.q.readw(slot + 12) == cid:
                raise AssertionError('doorbell 8-byte write was processed')
            time.sleep(0.01)

        # the proper 32-bit doorbell ring must submit the command
        dev.q.writel(dev.aq.sq_db, dev.aq.sq_tail)
        got = None
        deadline = time.time() + 5
        while time.time() < deadline:
            got = dev.q.readw(slot + 12)
            if got == cid:
                break
            time.sleep(0.001)
        check(got, cid, 'SQ doorbell 32-bit write after ignored 8-byte')

        # the 64-bit-programmed admin queues must work end to end
        ctrl = dev.identify_ctrl()
        vid = struct.unpack_from('<H', ctrl, ID_CTRL_VID)[0]
        check(vid, 0x1b36, 'Identify Controller over 8-byte-programmed queues')

        # final disable
        dev.disable()
        check(dev.is_rdy(), 0, 'RDY after final disable')

    # the fixed AERL retired the 'aerl' property: device creation must
    # now reject it outright instead of quietly ignoring the override.
    # (qtest connects before machine init, so the rejection arrives as
    # QEMU exiting on its own shortly after launch - poll for its exit
    # and read the captured stderr rather than assuming launch-time
    # failure.)
    tmp2 = os.path.join(tmp, 'aerl-prop')
    os.mkdir(tmp2)
    dev2 = NvmeDev(make_disk(tmp2), workdir=tmp2, device_opts='aerl=2')
    dev2.q.start()

    deadline = time.time() + 5
    while time.time() < deadline and dev2.q.proc.poll() is None:
        time.sleep(0.05)

    rc = dev2.q.proc.poll()
    dev2.q.stop(raise_on_error=False)
    if rc is None:
        raise AssertionError('vnvme accepted the removed aerl property')
    with open(dev2.q.stderr_path, 'rb') as f:
        stderr = f.read()
    if b'aerl' not in stderr:
        raise AssertionError(
            f'vnvme launch failed without naming the aerl property '
            f'(status {rc}): {stderr!r}')


def main():
    with tempfile.TemporaryDirectory(prefix='qootest-') as tmp:
        run(tmp)
    print('test_vnvme_admin: PASS')


if __name__ == '__main__':
    main()

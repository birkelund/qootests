#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
nvme (test device) Namespace Attachment regression test.

Ported from the bootests double-detach smoke test: the boot-level
`nvme detach-ns` / `attach-ns` round trip found a real bug (detaching an
already-detached namespace asserted; fixed by 31b737b19dca "hw/nvme:
fix namespace attachment"). That coverage now lives here, deterministically
and pinned to exact status codes instead of nvme-cli string matching.

Covers: OACS advertisement of Namespace Management, detach/reattach of
the default (legacy drive-backed) namespace, the double-detach and
double-attach rejections, invalid nsids (0, unallocated, broadcast),
invalid controller lists (empty, unknown controller id), an invalid
selection value, and that the namespace serves I/O again after being
reattached.

The vnvme production device does not implement Namespace Management
(OACS bit 3 is clear) and is covered by test_vnvme_admin; this test
targets the nvme test device only.
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nvme import (NvmeDev, make_disk, check, ID_CTRL_OACS, ID_CTRL_CNTLID,
                  NVME_OACS_NS_MGMT, build_rw, build_ns_attachment,
                  NS_ATTACHMENT_ATTACH, NS_ATTACHMENT_DETACH, BUF_ADDR,
                  DATA_ADDR, CMD_READ, ST_SUCCESS, ST_INVALID_FIELD,
                  ST_INVALID_NSID, ST_NS_ALREADY_ATTACHED,
                  ST_NS_NOT_ATTACHED, ST_NS_CTRL_LIST_INVALID, ST_DNR)


def run(tmp):
    disk = make_disk(tmp)
    with NvmeDev(disk, workdir=tmp, device='nvme', serial='nsmgmt') as dev:
        dev.setup_pci()

        aq = dev.enable()

        # the nvme test device must advertise Namespace Management
        ctrl = dev.identify_ctrl()
        oacs = struct.unpack_from('<H', ctrl, ID_CTRL_OACS)[0]
        check(oacs & NVME_OACS_NS_MGMT, NVME_OACS_NS_MGMT,
              'OACS advertises NS management')

        # controller id for the attachment controller list
        cntlid = struct.unpack_from('<H', ctrl, ID_CTRL_CNTLID)[0]

        # nsid 1 (the legacy drive-backed default namespace) is active
        lst = dev.identify_ns_list()
        check(struct.unpack_from('<1024I', lst)[0], 1,
              'Active Namespace List lists nsid 1')

        # detach nsid 1 from the controller
        sc = dev.ns_attachment(NS_ATTACHMENT_DETACH, 1, [cntlid])
        check(sc, ST_SUCCESS, 'detach nsid 1')

        # detaching an already-detached namespace must fail with
        # Namespace Not Attached (| DNR) -- previously this asserted
        sc = dev.ns_attachment(NS_ATTACHMENT_DETACH, 1, [cntlid])
        check(sc, ST_NS_NOT_ATTACHED | ST_DNR, 'double detach nsid 1')

        # the active namespace list must no longer list nsid 1
        lst = dev.identify_ns_list()
        if any(struct.unpack_from('<1024I', lst)):
            raise AssertionError('Active Namespace List not empty after '
                                 'detaching the only namespace')

        # reattach the namespace
        sc = dev.ns_attachment(NS_ATTACHMENT_ATTACH, 1, [cntlid])
        check(sc, ST_SUCCESS, 'reattach nsid 1')

        # attaching an already-attached namespace must fail with
        # Namespace Already Attached (| DNR)
        sc = dev.ns_attachment(NS_ATTACHMENT_ATTACH, 1, [cntlid])
        check(sc, ST_NS_ALREADY_ATTACHED | ST_DNR, 'double attach nsid 1')

        # nsid 1 is active again
        lst = dev.identify_ns_list()
        check(struct.unpack_from('<1024I', lst)[0], 1,
              'Active Namespace List lists nsid 1 after reattach')

        # the reattached namespace must serve I/O again: read one block
        # from lba 0 and verify it matches the (sparse, zeroed) disk
        io = dev.create_io_queue(1)
        dev.q.mem_zero(DATA_ADDR, 4096)
        _, sc = io.run(build_rw(CMD_READ, 1, 0, 1, prp1=DATA_ADDR))
        check(sc, ST_SUCCESS, 'read from reattached namespace')
        got = dev.q.mem_read(DATA_ADDR, 512)
        if any(got):
            raise AssertionError('unexpected non-zero data from lba 0')
        dev.delete_io_queue(1)

        # invalid nsid: 0 is never a valid namespace id
        sc = dev.ns_attachment(NS_ATTACHMENT_DETACH, 0, [cntlid])
        check(sc, ST_INVALID_NSID | ST_DNR, 'detach with nsid 0')

        # invalid nsid: within the allocated range but no such namespace
        sc = dev.ns_attachment(NS_ATTACHMENT_DETACH, 2, [cntlid])
        check(sc, ST_INVALID_FIELD | ST_DNR, 'detach of unallocated nsid 2')

        # invalid nsid: the broadcast value is not a valid attachment target
        sc = dev.ns_attachment(NS_ATTACHMENT_DETACH, 0xffffffff, [cntlid])
        check(sc, ST_INVALID_FIELD | ST_DNR, 'detach with broadcast nsid')

        # empty controller list (nr of ids = 0)
        dev.q.mem_zero(BUF_ADDR, 4096)
        _, sc = dev.aq.run(build_ns_attachment(NS_ATTACHMENT_DETACH, 1,
                                                BUF_ADDR))
        check(sc, ST_NS_CTRL_LIST_INVALID | ST_DNR, 'empty controller list')

        # unknown controller id
        sc = dev.ns_attachment(NS_ATTACHMENT_DETACH, 1, [0xdead])
        check(sc, ST_NS_CTRL_LIST_INVALID | ST_DNR, 'unknown controller id')

        # invalid selection value (only 0 = attach and 1 = detach exist)
        sc = dev.ns_attachment(2, 1, [cntlid])
        check(sc, ST_INVALID_FIELD | ST_DNR, 'invalid selection value')

        # the failed commands above must not have detached the namespace
        lst = dev.identify_ns_list()
        check(struct.unpack_from('<1024I', lst)[0], 1,
              'Active Namespace List still lists nsid 1')

        dev.disable()


def main():
    with tempfile.TemporaryDirectory(prefix='qootest-') as tmp:
        run(tmp)
    print('test_nvme_nsmgmt: PASS')


if __name__ == '__main__':
    main()

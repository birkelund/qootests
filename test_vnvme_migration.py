#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Migration regression test for the nvme/vnvme device state stream.

The test device ('nvme') implements savevm/migrate via the ctrl vmsd
(version 2: intx pending counter, cqueue vectors) and generic PCI state
(config, irq_level bitmap). The production device ('vnvme') registers
no vmstate and blocks migration outright.

For the test device the verification covers:

  * an enabled controller with a live I/O queue migrates, and the
    destination sees CSTS.RDY set and the PCI BARs intact
  * the pin level is reconstructed on the destination: a completion
    left pending on the source (pin asserted, intx pending counter > 0)
    must re-assert the pin on the destination after cutover
  * the pending completion is consumable on the destination, and
    draining a migrated queue deasserts the pin (the migrated pending
    counter and the cqueue vector/interrupt state stay in balance)
  * the controller is fully functional post-migration: new I/O and
    admin completions assert/deassert the pin through the migrated
    queues

For the production device it pins the blocker: vnvme registers no
vmstate, so migration is explicitly rejected ('vnvme device does not
support migration yet'). If vnvme ever grows vmstate, the blocker --
and this check -- must go.

The pin is observed via 'irq_intercept_in ioapic' on the q35 machine
(as in test_vnvme_intx.py). Migration is driven with a QMP socket per
QEMU (see QmpClient); memory contents migrate through the stream, but
the block backend does not, so the destination gets its own (empty)
disk image and the test checks status codes, not read data.

Driver-side queue state (indices, phase, outstanding cids) does not
migrate -- the snapshots taken on the source are replayed onto fresh
NvmeQueue objects on the destination to model a driver that tracks its
own state.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qtest import QmpClient, QTestError
from nvme import (NvmeDev, NvmeQueue, make_disk, check, build_rw,
                  build_get_features, ASQ_ADDR, ACQ_ADDR, IO_SQ_ADDR,
                  IO_CQ_ADDR, DATA_ADDR, DB_BASE, BAR0_DEFAULT, REG_CSTS,
                  CSTS_RDY, ST_SUCCESS, CMD_READ, FEAT_NUM_QUEUES, ID_NS_DS)

QSIZE = 16
NLB = 4
QUIET = 0.15


def snapshot(q):
    """Driver-side view of a queue (indices, phase, cids in flight)."""
    return {'sq_tail': q.sq_tail, 'cq_head': q.cq_head, 'phase': q.phase,
            'pending': list(q.pending), 'next_cid': q.dev.cid}


def attach(dev, snap, sq_addr, cq_addr, sq_db, cq_db):
    """Recreate driver-side queue state on a migrated device."""
    # continue the source's cid sequence: a fresh cid may otherwise
    # collide with the cids already committed to migrated (or still
    # zeroed) completion queue memory
    dev.set_next_cid(snap['next_cid'])
    q = NvmeQueue(dev, sq_addr, cq_addr, sq_db, cq_db, QSIZE)
    q.sq_tail = snap['sq_tail']
    q.cq_head = snap['cq_head']
    q.phase = snap['phase']
    q.pending = list(snap['pending'])
    return q


def run(tmp, device):
    src_work = os.path.join(tmp, 'src')
    dst_work = os.path.join(tmp, 'dst')
    os.makedirs(src_work)
    os.makedirs(dst_work)

    mig_sock = os.path.join(tmp, 'mig.sock')
    src_qmp = os.path.join(src_work, 'qmp.sock')
    dst_qmp = os.path.join(dst_work, 'qmp.sock')

    src_disk = make_disk(src_work)
    dst_disk = make_disk(dst_work)

    with NvmeDev(src_disk, workdir=src_work, device=device,
                 machine='q35',
                 qemu_args=['-qmp', f'unix:{src_qmp},server=on,wait=off']) as src:
        src.setup_pci()
        src.enable()

        if device == 'vnvme':
            # vnvme has no vmstate: migration is refused outright, the
            # device state could not ride the stream
            qmp = QmpClient(src_qmp)
            try:
                qmp.cmd('migrate', {'uri': f'unix:{mig_sock}'})
            except QTestError as e:
                check('vnvme device does not support migration yet' in str(e),
                      True, 'vnvme migrate rejected by the migration blocker')
            else:
                check(False, True, 'vnvme migrate must be rejected')
            qmp.close()
            return

        with NvmeDev(dst_disk, workdir=dst_work, device=device,
                     machine='q35',
                     qemu_args=['-qmp', f'unix:{dst_qmp},server=on,wait=off',
                                '-incoming', f'unix:{mig_sock}']) as dst:
            src.identify_ns(1)
            io = src.create_io_queue(1, irq_en=True)

            src.q.cmd('irq_intercept_in ioapic')
            src.q.irq_events.clear()
            dst.q.cmd('irq_intercept_in ioapic')
            dst.q.irq_events.clear()

            # leave a posted, unconsumed completion behind: submit a
            # read and wait for its CQE to land in the I/O CQ in guest
            # RAM (the completion itself, not the in-flight request,
            # must be migrated). The pin is asserted by the completion.
            cid = io.submit(build_rw(CMD_READ, 1, 0, NLB, prp1=DATA_ADDR))
            slot = IO_CQ_ADDR + io.cq_head * 16
            deadline = time.time() + 5
            while src.q.readw(slot + 12) != cid:
                if time.time() > deadline:
                    raise TimeoutError('pre-migration I/O completion did '
                                       'not post on the source')
            src.q.wait_irq('raise')      # precondition: pin asserted on src
            src.q.irq_events.clear()

            snap_aq = snapshot(src.aq)
            snap_io = snapshot(io)

            # the destination creates the incoming listener
            # asynchronously; racing it makes 'migrate' fail to connect
            t = time.time() + 10
            while not os.path.exists(mig_sock) and time.time() < t:
                time.sleep(0.05)
            qmp = QmpClient(src_qmp)
            qmp.cmd('migrate', {'uri': f'unix:{mig_sock}'})
            deadline = time.time() + 30
            while True:
                st = qmp.cmd('query-migrate')['status']
                if st in ('completed', 'failed'):
                    break
                if time.time() > deadline:
                    raise TimeoutError(f'migration stuck in "{st}"')
                time.sleep(0.01)
            check(st, 'completed', 'migration status')

            # the destination harness must use the BAR that migrated
            # over (same value we assigned on the source)
            dst.bar0 = BAR0_DEFAULT

            # the migrated controller is enabled and the PCI config
            # (BARs) survived the stream
            check(dst.reg_read(REG_CSTS) & CSTS_RDY, CSTS_RDY,
                  'CSTS.RDY on the destination')

            # the pin must be re-asserted on the destination: the
            # pending counter rode the ctrl v2 stream
            gsi = dst.q.wait_irq('raise')[1]

            # the pending completion is consumable, and draining
            # the migrated queue deasserts the pin
            io_dst = attach(dst, snap_io, IO_SQ_ADDR, IO_CQ_ADDR,
                            dst.bar0 + DB_BASE + 2 * 4,
                            dst.bar0 + DB_BASE + 3 * 4)
            _, sc = io_dst.wait_cqe()
            check(sc, ST_SUCCESS, 'migrated I/O completion')
            ev = dst.q.wait_irq('lower')
            check(ev[1], gsi, 'pin deasserted on drain')

            # the migrated controller takes new work: through the
            # migrated io queue and the admin queue alike. Also
            # proves the migrated cqueue vector/interrupt state.
            io_dst.submit(build_rw(CMD_READ, 1, 0x40, NLB,
                                   prp1=DATA_ADDR))
            _, sc = io_dst.wait_cqe()
            check(sc, ST_SUCCESS, 'post-migration I/O')
            ev = dst.q.wait_irq('raise')
            check(ev[1], gsi, 'pin re-asserted on new I/O')
            ev = dst.q.wait_irq('lower')
            check(ev[1], gsi, 'pin deasserted again')

            aq_dst = attach(dst, snap_aq, ASQ_ADDR, ACQ_ADDR,
                            dst.bar0 + DB_BASE,
                            dst.bar0 + DB_BASE + 4)
            _, sc = aq_dst.run(build_get_features(FEAT_NUM_QUEUES))
            check(sc, ST_SUCCESS, 'post-migration admin command')
            ev = dst.q.wait_irq('raise')
            check(ev[1], gsi, 'admin completion asserts the pin')
            ev = dst.q.wait_irq('lower')
            check(ev[1], gsi, 'admin drain deasserts the pin')
            dst.q.assert_no_irq(QUIET)

            qmp.close()


def main():
    for device in ('nvme', 'vnvme'):
        with tempfile.TemporaryDirectory(prefix='qootest-') as tmp:
            run(tmp, device)
        print(f'test_vnvme_migration[{device}]: PASS')


if __name__ == '__main__':
    main()

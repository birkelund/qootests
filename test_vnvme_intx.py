#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Pin-based interrupt (INTx) regression test (vnvme and nvme).

Covers the INTx delivery that the rest of the suite cannot see (all
other tests observe completions by polling CQ memory):

  * a completion on a queue with interrupts enabled asserts the pin,
    draining the queue deasserts it
  * batched completions assert the pin; the transitions must strictly
    alternate (level-based signaling must not re-assert while the pin is
    already high) and the queue drain must end deasserted. The
    accounting must stay balanced across batches -- an over- or
    under-decrement of the internal pending counter produces a missing
    or spurious raise/lower transition here
  * INTMS masks the asserted pin, INTMC unmasked re-asserts it while a
    completion is still pending, and the INTMC register mirrors INTMS
  * completions arriving while masked never assert the pin

The pin is observed with 'irq_intercept_in ioapic' on the q35 machine
(on the i440fx 'pc' machine PCI INTx does not route through the ioapic),
which makes every level transition emit an asynchronous 'IRQ raise N' or
'IRQ lower N' message; QemuQTest.cmd() collects those into
QemuQTest.irq_events without disturbing the command/response pairing.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nvme import (NvmeDev, make_disk, check, build_rw, build_get_features,
                  ID_NS_DS, ST_SUCCESS, REG_INTMS, REG_INTMC, DATA_ADDR,
                  CMD_READ, FEAT_NUM_QUEUES)

BATCH = 4                        # completions per I/O batch
NLB = 4                          # blocks per read
QUIET = 0.15                     # seconds to keep listening for stray events


def run(tmp, device):
    disk = make_disk(tmp)
    with NvmeDev(disk, workdir=tmp, device=device, machine='q35') as dev:
        dev.setup_pci()
        dev.enable()

        bs = 1 << dev.identify_ns(1)[ID_NS_DS]
        io = dev.create_io_queue(1, irq_en=True)

        # from here on, every pin level transition must turn up as an
        # IRQ raise/lower event (the intercept makes the ioapic inputs
        # observable; the first raise tells us which gsi our INTA is)
        dev.q.cmd('irq_intercept_in ioapic')
        dev.q.irq_events.clear()

        def expect_pin(kind, gsi):
            ev = dev.q.wait_irq(kind)
            check(ev[1], gsi, f'IRQ {kind} on the same pin')
            dev.q.assert_no_irq(QUIET)

        def check_batch_transitions(gsi):
            """All pin transitions seen during an I/O batch: the batch
            must start with a raise and end with a lower (the pin is low
            when the batch starts, and comes back down when the queue is
            fully drained) and must strictly alternate -- e.g. a missing
            decrement on drain leaves the batch ending on a raise, and a
            double decrement makes the next batch produce no transitions
            at all."""
            evs = dev.q.irq_events
            dev.q.irq_events = []
            kinds = [e[0] for e in evs]
            if not evs or kinds[0] != 'raise' or kinds[-1] != 'lower':
                raise AssertionError(f'unbalanced pin transitions for the '
                                     f'batch: {evs}')
            for a, b in zip(kinds, kinds[1:]):
                if a == b:
                    raise AssertionError(f'pin did not alternate: {evs}')
            for _, g in evs:
                check(g, gsi, 'IRQ on the same pin')
            dev.q.assert_no_irq(QUIET)

        # an admin completion asserts the pin; consuming the CQE (ringing
        # the ACQ head) deasserts it
        _, sc = dev.aq.run(build_get_features(FEAT_NUM_QUEUES))
        check(sc, ST_SUCCESS, 'Get Features (INTx)')
        gsi = dev.q.wait_irq('raise')[1]
        expect_pin('lower', gsi)

        # batched I/O completions: alternating transitions, ending low --
        # twice, to catch unbalanced pending accounting that only shows
        # up on the next cycle
        for batch in range(2):
            for i in range(BATCH):
                io.submit(build_rw(CMD_READ, 1, 0x100 + batch + i * NLB,
                                   NLB, prp1=DATA_ADDR))
            for _ in range(BATCH):
                _, sc = io.wait_cqe()
                check(sc, ST_SUCCESS, f'I/O completion (batch {batch})')
            check_batch_transitions(gsi)

        # INTMS masks the asserted pin; INTMC re-asserts it because the
        # completion is still pending (the pin is level-based)
        io.submit(build_rw(CMD_READ, 1, 0x200, NLB, prp1=DATA_ADDR))
        dev.q.wait_irq('raise')
        dev.reg_write(REG_INTMS, 1)
        check(dev.reg_read(REG_INTMS), 1, 'INTMS readback')
        check(dev.reg_read(REG_INTMC), 1, 'INTMC mirrors INTMS')
        expect_pin('lower', gsi)
        dev.reg_write(REG_INTMC, 1)
        check(dev.reg_read(REG_INTMS), 0, 'INTMC clears INTMS')
        expect_pin('raise', gsi)
        _, sc = io.wait_cqe()
        check(sc, ST_SUCCESS, 'I/O completion through the mask cycle')
        expect_pin('lower', gsi)

        # completions arriving while the pin is masked must not assert
        # it, and unmasking with nothing pending must not assert it
        dev.reg_write(REG_INTMS, 1)
        io.submit(build_rw(CMD_READ, 1, 0x300, NLB, prp1=DATA_ADDR))
        _, sc = io.wait_cqe()
        check(sc, ST_SUCCESS, 'I/O completion while masked')
        dev.reg_write(REG_INTMC, 1)
        dev.q.assert_no_irq(QUIET)

        dev.disable()


def main():
    for device in ('vnvme', 'nvme'):
        with tempfile.TemporaryDirectory(prefix='qootest-') as tmp:
            run(tmp, device)
        print(f'test_vnvme_intx[{device}]: PASS')


if __name__ == '__main__':
    main()

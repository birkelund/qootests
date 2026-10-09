# qootests

A standalone regression suite for the QEMU NVMe device emulation
(`hw/nvme`), driven over the qtest wire protocol. No test framework
(no pytest), no external dependencies — each `test_*.py` is a plain
Python 3 script that exits non-zero on failure and prints
`<name>: PASS` on success.

The suite targets both device models in the QEMU tree:

- **vnvme** — the production device model
- **nvme** — the test device model (used for CMB/PMR, migration and
  other behaviors vnvme does not implement)

## Prerequisites

- A built QEMU tree at `../qemu/build/qemu-system-x86_64` (relative to
  this repository). Override with the `QOOTEST_QEMU` environment
  variable if the binary lives elsewhere:

  ```sh
  QOOTEST_QEMU=/path/to/qemu-system-x86_64 ./run.sh
  ```

- The QEMU build must contain the `vnvme` and `nvme` device models
  (`hw/nvme`).

## Running

Run all tests:

```sh
./run.sh
```

Run a single test (from the repo root):

```sh
python3 test_vnvme_io.py
```

Each test creates its own temporary workdir and sparse disk image; no
setup or cleanup is needed. QEMU stderr is captured to
`<workdir>/qemu-stderr.log` and included in error messages.

## Tests

| Test | Device(s) | Coverage |
| ---- | --------- | -------- |
| `test_vnvme_admin.py` | vnvme | Admin path: PCI BAR setup, controller enable/disable, Identify, Get Features, invalid opcodes |
| `test_vnvme_io.py` | vnvme | I/O path: I/O queues, write/read integrity, OOB rejection, Write Zeroes, Flush, DSM, NSID enforcement, reset recovery |
| `test_vnvme_dbbuf.py` | vnvme, nvme | Shadow doorbell buffer (DBBUF): config validation, shadow doorbells and event indices, reset teardown; each with ioeventfd off and on |
| `test_vnvme_intx.py` | vnvme, nvme | Pin-based interrupts (INTx) on q35: pin assert/deassert, batching, level signaling |
| `test_vnvme_cmb.py` | nvme | Controller Memory Buffer: legacy-cmb and v1.4 CMBMSC schemes, guest access, I/O through the CMB, SQ in CMB |
| `test_vnvme_dptr.py` | vnvme, nvme | Data-pointer placement: buffers in foreign device MMIO (rejection vs bouncing) and in the PMR |
| `test_vnvme_migration.py` | vnvme (blocker), nvme | Migration: live state on the destination, pending INTx reconstruction, and the explicit vnvme migration blocker |

## Layout

- `run.sh` — runs every `test_*.py`, exits non-zero if any fail
- `qtest.py` — minimal qtest protocol client and QEMU runner
  (`QemuQTest`), plus a minimal QMP client (`QmpClient`) for the
  migration test
- `nvme.py` — NVMe helper layer: `NvmeDev`, `NvmeQueue` (CID-matched
  CQE polling, no fixed sleeps), 64-byte command builders, and
  register/status constants matching `include/block/nvme.h` in the
  QEMU tree
- `test_*.py` — self-contained regression tests

## Design notes

- There is no BIOS under qtest, so tests configure PCI BARs and the
  command register manually through the 0xcf8/0xcfc config ports
  (`NvmeDev.setup_pci()`).
- Completions are consumed by polling CQ memory for a matching CID
  rather than sleeping, so tests are robust to queue re-creation over
  stale completion memory.
- The migration test drives a second QEMU over QMP; block backend
  contents do not migrate, so the destination gets its own disk and
  the test checks status codes, not read data.

Companion suites: `../bootests` (guest boot tests) and the QEMU tree
at `../qemu` — device fixes there usually come with coverage added
here. See `AGENTS.md` for repo conventions and testing gotchas.

## License

GPL-3.0-or-later; see [COPYING](COPYING).

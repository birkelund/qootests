# qootests

Standalone Python 3 regression suite for the QEMU NVMe device emulation,
driven over the qtest wire protocol. No test framework (no pytest), no
external dependencies, no package manifest. Each `test_*.py` is a plain
script: it exits non-zero on failure and prints `<name>: PASS` on success.

## Prerequisites

- Requires a built QEMU tree at `../qemu/build/qemu-system-x86_64`
  (i.e. `/home/kjensen/src/qemu` next to this repo). Override with the
  `QOOTEST_QEMU` environment variable if the binary lives elsewhere.
- Tests need the `vnvme` (production) and `nvme` (test) device models in
  that QEMU build (`hw/nvme`).

## Running

- All tests: `./run.sh` (loops over `test_*.py`, exit 1 if any fail).
- One test: `python3 test_vnvme_io.py` — run scripts directly from the
  repo root; they insert their own directory into `sys.path`.

## Architecture

- `qtest.py` — minimal qtest protocol client + QEMU runner (`QemuQTest`)
  and a minimal QMP client (`QmpClient`, used by the migration test).
  Non-obvious points:
  - The test side is the unix socket *server*; QEMU connects to us.
  - No BIOS with qtest: PCI BARs and command register must be set up
    manually via the 0xcf8/0xcfc config ports (`setup_pci()`).
  - QEMU stderr is captured to `<workdir>/qemu-stderr.log` and appended
    to error messages; a QEMU crash surfaces as the connection closing.
  - `with` blocks call `stop(raise_on_error=...)` so an in-flight test
    failure is never masked by a shutdown error.
- `nvme.py` — NVMe helper layer: `NvmeDev` (default device `vnvme`, fixed
  PCI addr 04.0), `NvmeQueue` (CID-matched CQE polling, no fixed sleeps),
  64-byte command builders, and register/status constants matching
  `include/block/nvme.h` in the QEMU tree.
- `test_*.py` — self-contained tests; they create their own temp
  workdirs and sparse disk images (`make_disk`).

## Conventions and gotchas

- `wait_cqe()`/`run()` return `(result, sc)` where `sc` is the 16-bit
  status from the CQE *including* the DNR bit — e.g. expected values are
  `ST_LBA_RANGE | ST_DNR`, not bare `ST_LBA_RANGE`.
- CIDs are allocated monotonically per device and never reused; the CQE
  matcher accepts any pending CID at the head slot (completions can
  overtake each other). Migration tests must continue the source's CID
  sequence via `set_next_cid()` or stale CQ memory can collide.
- `device_opts` is interpolated into QEMU argv — `;` and leading/trailing
  commas are rejected by assert in `NvmeDev.__init__`.
- Default machine is `pc` with 128MB guest RAM at a fixed guest memory
  layout (see the address constants at the top of `nvme.py`); the intx
  and migration tests use `q35` with `irq_intercept_in ioapic`
  observed via `q.irq_events` / `wait_irq()`.
- Migration test: block backend contents do not migrate — the
  destination gets its own disk and only status codes are checked.
- Some tests pin deliberately asymmetric device behavior (e.g. vnvme has
  no Format NVM support, vnvme blocks migration). If device behavior
  changes in the QEMU tree, the affected checks here are meant to be
  updated along with it.
- Companion suites: `../bootests` (guest boot tests) and the QEMU tree
  at `../qemu` — device fixes there usually come with coverage added
  here.

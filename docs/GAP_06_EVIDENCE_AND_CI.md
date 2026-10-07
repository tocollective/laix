# G6. Evidence is tied to specific artifacts and parts of CI have not run

[Gap checklist](GAPS_CHECKLIST.md) · [Previous](GAP_05_KERNEL_LATENCY.md) · [Next](GAP_07_APPLICATION_LAYER.md)

Date: 2026-10-07. Priority: **P2**.

## Criterion

An acceptance claim belongs to the exact source, compiler, image, emulator
and ROM that were tested. Claims must not be carried to changed code.

## Current state

From [AUDIT_09_ACCEPTANCE_AND_CI.md](AUDIT_09_ACCEPTANCE_AND_CI.md) and
[ACCEPTANCE_CI.md](../tests/ACCEPTANCE_CI.md):

- **Resolved 2026-10-07.** The A9 campaign predated the `mc/runtime/rt.m`
  `putc` change. A new campaign on the current tree (root `4736b7d`, mc
  `b6f16b6`, laix `8cf90d6`) passed 399 source tests and all 14 CPU profiles
  (20 probe suites) with the same approved `wrm081632` and `firmware.rom`
  bytes; nothing was rebuilt. `acceptance_provenance.py --verify` passes on
  that tree, and the A9 snapshot still replays. Caveat: the record binds git
  HEADs, so it verifies only until the next commit.
- GitHub Actions was not run remotely in the recorded workspace. Source CI is
  configured for every push. CPU CI needs four repository variables
  (`LAIX_TOOLS_RUN_ID`, `LAIX_TOOLS_ARTIFACT`, `LAIX_EMULATOR_SHA256`,
  `LAIX_ROM_SHA256`); without them it is skipped, not passed.
- The source evaluator does not establish compiled output, device timing or
  real interleavings. Stress is bounded (counts and virtual CPU time), not
  indefinite.
- The A10 selection record (this audit round) changed only documents and was
  not run against any suite.

## Fix directions

- ~~Re-run the source suite and the artifact replay on the current tree and
  re-record provenance; resolve or accept the `rt.m` change explicitly.~~ Done
  (see Current state). Still open: a pinned snapshot of this tree, so
  verification survives commits.
- Provision the four CI variables and complete one remote run; record the run
  id as evidence.
- Keep input bundles while a readiness claim is maintained.
- ~~Add a longer soak profile for the finite-lifetime and reaping workloads.~~
  Done 2026-10-08: profile `soak` (`LAIX_CONSOLE=soak`, image
  `src/kernel/soak_bootstrap.asm`). One user supervisor runs 4096 child
  lifetimes, every eighth a fault, and checks each round's `SYS_LIFETIME`
  report (exactly one task-reference generation spent, no reply identity spent,
  no retired slot) and completion event. The probe then checks the end state:
  nothing live or leaked, slot 2 at generation 4095, other slots unused, and
  the last 32 history events. It ran in about 80 s locally and fails when a
  supervisor check is broken. Limits: it exercises the task path only, so reply
  namespaces are covered by the `lifetime` scenario, not here; it is bounded
  (4096 rounds, about 0.5% of one slot's 8,388,608 generations), not
  indefinite.
- Per project rules, WRM and ROM are not rebuilt to verify; use existing
  approved binaries.

## Done when

The readiness matrix cites a current passing run on the current source, the
remote CI result is recorded, and no row remains "validation pending" for
reasons other than a scoped, documented limitation.

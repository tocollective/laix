# A9. Acceptance and CI need to follow the current architecture

[Audit overview](LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_08_LIMITS_AND_LATENCY.md) · [Next item](AUDIT_10_OPTIONAL_EXTENSIONS.md)

Date: 2026-10-04. Priority: **P2**.

These checklists describe proposed work, not completed implementation.

## Dependencies

Baseline work can start immediately. Every new API in A1–A8 needs both source-model regression and matching CPU acceptance as it lands.

## Current state and gap

Existing tests distinguish checked M/ASM execution from real CPU/device runs,
which is valuable. The source evaluator uses CPU/IRQ/device fixtures; passing
it does not establish compiler output, hardware timing or real instruction
interleavings. Historical CPU probes use monitor-controlled scenarios and
record hashes; their conclusions apply to those artifacts and cases.

Remaining or incomplete evidence includes:

- Standalone Raw send/receive CPU acceptance remains pending in
  [stage 5](05_IPC_RIGHTS.md).
- Multi-client UART/screen ordering and extended stress with real application
  images remain open in [console acceptance](../tests/CONSOLE_SERVICE_ACCEPTANCE.md)
  and [screen acceptance](../tests/SCREEN_IRQ_DMA_ACCEPTANCE.md).
- IRQ boundary/coalescing fixtures need complementary forced CPU/device
  timing scenarios. The safety tests already cover many source boundaries.
- The [simple-service CPU report](../tests/SIMPLE_SERVICES_ACCEPTANCE.md)
  exercises an outstanding DMA reservation at fault, but does not guarantee
  BUSY at the exact death instruction. Source tests deliberately hold BUSY.
  CPU tests still need active-transfer death, timeout/late completion,
  post-quiescence canaries and media replacement/removal.
- Natural headless Input acceptance sees an empty keyboard FIFO. Real HID
  arrival/overflow under concurrent clients is fixture-tested, not injected
  host-event CPU acceptance.
- [The root CI workflow](../../.github/workflows/ci.yml) does not run LA/IX's
  source suite or its CPU acceptance profiles. Add dedicated LA/IX checks
  and preserve the emulator/ROM/image/source provenance of each run.

Documentation also needs one current readiness matrix. For example,
[KERNEL.md](KERNEL.md) still leaves stage 6 incomplete while the
newer stage-6/simple-service documents record completed failure milestones;
the bootstrap document calls implemented screen work subsequent work. Report
completed containment, pending stress and unimplemented restart separately.
The old request/reply helper CPU checkbox is historical: newer simple-service
images use the compiled M `accept` helper. Do not count the older limitation
as proof that no user service executes that helper today.

**Acceptance:** CI runs the source suite; maintained artifact-based CPU suites
cover all supported profiles; each readiness claim links to a concrete test
and matching artifacts. New lifecycle APIs add CPU tests for races, exhaustion,
rollback and repeated resource reuse.

## Implementation approach

Treat the checked-source evaluator and CPU probes as complementary tools.
The evaluator reaches boundary states cheaply; the CPU runs validate trap
assembly, compiled code, real MMU/device execution and timing assumptions.
Neither one alone covers all of those properties.

A no-build acceptance job should consume identified emulator, ROM, image and
map artifacts. A separate artifact-production workflow can build LA/IX as
allowed by project rules. This audit did not build any project, and the rule
against verifying WRM by rebuilding it remains applicable.

## Implementation checklist

- [ ] Create one maintained matrix mapping each mechanism, limitation and recovery claim to source and CPU evidence.
- [ ] Distinguish implementation complete, source accepted, CPU accepted and sustained-stress accepted states.
- [ ] Add LA/IX source-suite execution to CI with all required submodules and deterministic failure reporting.
- [ ] Add artifact-based CPU acceptance jobs for supported UART, screen and simple-service profiles using approved emulator/ROM artifacts.
- [ ] Record source revisions or source manifests alongside image/map/tool/emulator/ROM hashes and probe versions.
- [ ] Add standalone Raw transport CPU scenarios for both arrival orders, cancellation, rights, capacity and lifecycle.
- [ ] Add multi-client UART/screen CPU ordering and long timer-preemption stress with real user application images.
- [ ] Add forced IRQ boundary/shared-line CPU/device scenarios, supplementing existing source fixtures.
- [ ] Guarantee BUSY at DMA owner death in a dedicated CPU case; test timeout, late completion and post-quiescence canaries.
- [ ] Add media removal/replacement and actual HID arrival/overflow CPU/device cases.
- [ ] Update stale roadmap/helper checkboxes using newer acceptance evidence without rewriting historical artifact claims.
- [ ] Add runtime lifecycle/recovery regressions for every new feature as it is implemented, rather than deferring all tests.

## Acceptance checklist

- [ ] CI fails on a LA/IX source regression and publishes useful logs plus artifact/source provenance.
- [ ] Each claimed CPU milestone can be rerun from documented existing inputs without rebuilding WRM for verification.
- [ ] The readiness matrix links to current passing evidence or clearly labels pending validation.
- [ ] CPU stress proves progress and state invariants across repeated IPC, failure, teardown and restart cycles.
- [ ] Device timing cases demonstrate their preconditions explicitly rather than infer BUSY/IRQ state from a reservation alone.
- [ ] Historical source-test counts and artifact hashes remain labeled by run; current results do not silently certify older images.

## Completion record

When work is accepted, link each completed requirement to its tests and record
the source and image provenance, remaining limitations and CPU acceptance
results. A checked box must not imply a stronger evidence level than the linked
record establishes.

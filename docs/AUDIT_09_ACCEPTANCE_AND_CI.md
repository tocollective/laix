# A9. Acceptance and CI need to follow the current architecture

[Audit overview](LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_08_LIMITS_AND_LATENCY.md) · [Next item](AUDIT_10_OPTIONAL_EXTENSIONS.md)

Date: 2026-10-04. Priority: **P2**.

Implementation and local acceptance are complete for the scoped A9 campaign.
The completion record below separates local evidence from remote CI configuration.

## Dependencies

Baseline work can start immediately. Every new API in A1–A8 needs both source-model regression and matching CPU acceptance as it lands.

## Original state and gap (2026-10-04)

The audit originally identified the following gaps. The dated historical
reports remain unchanged; current evidence is in the completion record.

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

- [x] Create one maintained matrix mapping each mechanism, limitation and recovery claim to source and CPU evidence.
- [x] Distinguish implementation complete, source accepted, CPU accepted and sustained-stress accepted states.
- [x] Add LA/IX source-suite execution to CI with all required submodules and deterministic failure reporting.
- [x] Add artifact-based CPU acceptance jobs for supported UART, screen and simple-service profiles using approved emulator/ROM artifacts.
- [x] Record source revisions or source manifests alongside image/map/tool/emulator/ROM hashes and probe versions.
- [x] Add standalone Raw transport CPU scenarios for both arrival orders, cancellation, rights, capacity and lifecycle.
- [x] Add multi-client UART/screen CPU ordering and long timer-preemption stress with real user application images.
- [x] Add forced IRQ boundary/shared-line CPU/device scenarios, supplementing existing source fixtures.
- [x] Guarantee BUSY at DMA owner death in a dedicated CPU case; test timeout, late completion and post-quiescence canaries.
- [x] Add media removal/replacement and actual HID arrival/overflow CPU/device cases.
- [x] Update stale roadmap/helper checkboxes using newer acceptance evidence without rewriting historical artifact claims.
- [x] Add runtime lifecycle/recovery regressions for every new feature as it is implemented, rather than deferring all tests.

## Acceptance checklist

- [x] CI fails on a LA/IX source regression and publishes useful logs plus artifact/source provenance.
- [x] Each claimed CPU milestone can be rerun from documented existing inputs without rebuilding WRM for verification.
- [x] The readiness matrix links to current passing evidence or clearly labels pending validation.
- [x] CPU stress proves progress and state invariants across repeated IPC, failure, teardown and restart cycles.
- [x] Device timing cases demonstrate their preconditions explicitly rather than infer BUSY/IRQ state from a reservation alone.
- [x] Historical source-test counts and artifact hashes remain labeled by run; current results do not silently certify older images.

## Completion record

Local completion: 2026-10-06. The preserved campaign uses its UTC run date.

The maintained [readiness matrix](READINESS_MATRIX.md) maps implementation,
source, CPU and bounded sustained-stress states to concrete tests and limitations.
[Acceptance and CI contracts](../tests/ACCEPTANCE_CI.md) document all 14 supported
profiles, approved-tool configuration, existing-input replay and artifact retention.
[The campaign provenance](../tests/ACCEPTANCE_CI_PROVENANCE.json) binds the accepted
source/compiler/probe manifest to every image, map, ELF, font, emulator, ROM and log.

| Completed requirement | Current implementation and evidence |
| --- | --- |
| Source CI, deterministic failure reports and provenance | [Root CI](../../.github/workflows/ci.yml), [source runner](../tests/run_source_suite.py), [failure/preflight regressions](../tests/test_acceptance_infrastructure.py) |
| Approved-input production and no-build CPU CI | [Reusable/dispatchable workflow](../../.github/workflows/laix-acceptance.yml), [LA/IX-only producer](../tools/build_acceptance_bundle.sh), [bundle verifier/runner](../tests/acceptance_bundle.py) |
| Raw transport, both arrival orders, rights/capacity/cancellation/lifecycle | [Raw CPU probe](../tests/probe_raw_transport_cpu.py), UART profile, 128 maximal rendezvous |
| Concurrent compiled UART/Screen applications and long timer progress | [Multi-client CPU probe](../tests/probe_multiclient_cpu.py), four compiled clients per profile, 128 writes and one-second hardware sleeps per client |
| Forced shared IRQ wait/rearm boundaries | [Device-event CPU probe](../tests/probe_device_events_cpu.py), Screen profile, actual DONE/VBLANK level, blocked wait, held-level mask, acknowledgement/retry and framebuffer progress |
| Active-transfer death, timeout/late completion and canaries | [Simple-service CPU probe](../tests/probe_simple_services_cpu.py), Services profile, physical BUSY at the fault and actual DEAD store, six-second transfer/five-second wait, pins to quiescence and later canaries |
| Medium removal/replacement and actual HID overflow | [Device-event probe](../tests/probe_device_events_cpu.py), Media profile; [HID probe](../tests/probe_hid_cpu.py), five compiled clients, 64 host events, 32 retained/32 dropped, 40 requests |
| Runtime API/recovery race, exhaustion, rollback and reuse regressions | Current Memory/Sharing/Objects/Supervisor/Recovery/Latency profiles and their linked source tests in [the matrix](READINESS_MATRIX.md); future APIs must extend both evidence levels in the same change |
| Roadmap/helper corrections and historical scope | [Kernel roadmap](KERNEL.md), [bootstrap](BOOTSTRAP.md), [stage 5](05_IPC_RIGHTS.md), [stage 6](06_USER_SERVICES.md), [request/reply helper](IPC_REQUEST_REPLY.md); older report hashes/counts remain attached to their original runs |

Local verification uses existing WRM and ROM bytes; neither was rebuilt.
The final source suite passed **399 tests**; all **14 CPU profiles / 20 probe
suites passed**. Detailed results and hashes are recorded in the linked
provenance and acceptance report. A concurrent `mc/runtime/rt.m` edit appeared after acceptance and was left intact.
It is validation-pending: preflight rejects the later working tree. A separate
content-verified source snapshot with pinned revisions replays the accepted
inputs, and full provenance verification passes there. Commands and both runtime
hashes are recorded in the acceptance report.
Earlier failed/interrupted attempts remain
separate and are not counted as acceptance.

Remote GitHub Actions execution was not performed in this workspace. Source CI
is configured for every push/PR. Automatic CPU CI requires the four approved Linux
tool repository variables documented in the acceptance report; an unconfigured
CPU call is skipped, rather than certified. Its failure behavior and local no-build
runner were verified locally. These checked CI requirements describe implemented
jobs and their tested contracts, rather than a claim of a completed remote run.

The stress claim covers repeated IPC/timer progress, task teardown/reuse and the
explicit five-generation Disk/Files recovery workload. It does not implement fixed
UART/Screen automatic restart, permanently BUSY hardware reset, or Screen medium
swaps between bitmap halves/cache-hit validation. Those scoped limitations remain
visible in the matrix. Historical source counts and artifact hashes do not certify
new images, and source fixtures do not substitute for CPU/device timing evidence.

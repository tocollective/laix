# A9 acceptance and CI

The [readiness matrix](../docs/READINESS_MATRIX.md) is the current entry point.
The maintained runner packages LA/IX inputs and executes probes without
building WRM or ROM. The source evaluator and CPU/device execution retain
separate results and evidence levels.

## CI contracts

[Root CI](../../.github/workflows/ci.yml) runs the entire LA/IX source suite on
every push/pull request with recursive submodules and Python 3.13. Discovery is
sorted by unittest; per-test identifiers, subtest failures, import errors,
tracebacks, elapsed time, Python version, git revisions and file manifests are
published even when the job fails. A changing source snapshot also fails.
`test_acceptance_infrastructure.py` exercises regression/error reporting and
rejects corrupt, missing, foreign-profile and unsafe bundle inputs.

[LAIX artifact acceptance](../../.github/workflows/laix-acceptance.yml) has
separate production and no-build CPU jobs for 16 profiles: UART, Screen,
Services, their UART/Screen stress fixtures, Memory, Sharing, Objects,
Supervisor, Soak, Loader, Recovery fixture/production, Latency, HID and Media. Soak
postdates the G6 campaign below (14 profiles), which did not run it. Production
builds only LA/IX and its applications; it downloads an existing approved Linux
`wrm081632` and `firmware.rom`, requires their explicit SHA-256 identities,
and never invokes CMake or a WRM/firmware compiler.

To enable these CPU checks from root CI, set repository variables
`LAIX_TOOLS_RUN_ID`, `LAIX_TOOLS_ARTIFACT`, `LAIX_EMULATOR_SHA256`, and
`LAIX_ROM_SHA256`. Without those approved inputs the source job still runs;
the CPU call is visibly skipped, not reported as passed. The tool artifact
must contain both files at its root. It must run on `ubuntu-latest`, including
its runtime dependencies, and support monitor/snapshot, `--input` and
`--deterministic`. A mismatched hash fails before executing it.

The workflow can also be dispatched with those four inputs. To replay existing
LA/IX inputs, supply only `bundle_run_id` and select the same source revision:
production is skipped and CPU jobs download that run's immutable bundles.
Different checkout/compiler/probe manifests fail preflight. GitHub artifact
upload/download loses executable permissions, so the verified binary's owner
execute bit is restored before the probes run
([artifact action documentation](https://github.com/actions/download-artifact/tree/v4#permission-loss)).

Every CPU job publishes wrapper output, raw probe logs, probe JSON and its
bundle/source manifest on failure as well as success. A child failure or bounded
process timeout fails the profile; later independent suites still run. Keep the
input bundles while the associated readiness claim remains maintained. An
expired/missing artifact requires producing new identified inputs and new
acceptance, rather than silently borrowing a historical report.

## Existing-input replay

From a checkout matching the recorded source manifest, no build is needed:

```sh
python3 -B laix/tests/acceptance_bundle.py verify \
  laix/build/acceptance/g6/complete/inputs/uart
python3 -B laix/tests/acceptance_bundle.py run \
  --bundle laix/build/acceptance/g6/complete/inputs/uart \
  --profile uart --log-dir laix/build/acceptance/g6/replay/uart
python3 -B laix/tests/run_source_suite.py \
  --log-dir laix/build/acceptance/g6/replay/source
python3 -B laix/tools/acceptance_provenance.py --verify
```

After the A9 run, `putc` was added to `mc/runtime/rt.m`. The G6 campaign
(2026-10-07) validated that later source with a new source suite and new LA/IX
inputs for all 14 profiles. The checked-in provenance record now describes the
G6 campaign (bundles and logs under `laix/build/acceptance/g6/complete/`). The
record binds git HEAD revisions as well as file contents, so `--verify` passes
only for the working tree and revisions it was recorded on; after new commits,
replay needs a pinned snapshot like the A9 one below. Its `date` field reads
`2026-10-05` because `acceptance_provenance.py` writes a constant; the campaign
date is 2026-10-07. The record was not edited by hand.

The A9 sources are preserved separately, with independent git metadata
pinned to the recorded revisions and file hashes recorded in provenance:

```sh
python3 -B laix/build/acceptance/a9/accepted-source/laix/tools/acceptance_provenance.py --verify
python3 -B laix/build/acceptance/a9/accepted-source/laix/tests/acceptance_bundle.py verify \
  laix/build/acceptance/a9/complete/inputs/uart
```

Use the same `accepted-source/laix/tests/acceptance_bundle.py` entry point for
`run` to replay the accepted CPU inputs without changing the concurrent MC edit.
Its build-directory link addresses the preserved campaign artifacts. The
accepted runtime hash is
`e941e357111971cffe4037123c6346516012891ae4706bf2bb38b4c92ed1ecb6`;
the later runtime hash observed at the final check is
`ca73fd99b9bf420f6da4c0541bf4efc8266b1afb94dc74d89b7d9888b8da108a`.
A new acceptance of the later source must produce new LA/IX inputs and rerun
the suites; historical bundles are not relabeled.

Replace `uart` with any supported profile. The runner restores only verified
generated images/maps/ELFs/font resources needed by existing probes. Run profiles
sequentially in one checkout: their generated ancillary paths are shared. CI
matrix jobs use isolated checkouts. Local probes need a loopback monitor socket;
all emulator machines use temporary copies of the disks and disable networking.

`tools/build_acceptance_bundle.sh PROFILE NEW_DIRECTORY EMULATOR ROM` is the
separate LA/IX artifact producer. The destination must be new. Its explicit
source-binding assertion is valid only immediately after that LA/IX production,
with the recorded sources unchanged. Do not use it to relabel older images.

## Current campaign and scope

[ACCEPTANCE_CI_PROVENANCE.json](ACCEPTANCE_CI_PROVENANCE.json) records the accepted
source snapshot, image/map/ELF/font/probe/tool hashes and profile results.
Raw bundles and logs are under `build/acceptance/g6/complete/` (G6, current) and
`build/acceptance/a9/complete/` (A9, historical), outside Git. The timing and
profile figures below are from A9; G6 reproduced the same counts (399 tests in
526 s source time, 14 profiles / 20 suites).
The checked-in record can verify those preserved files; it does not recreate
missing executable artifacts. WRM and ROM were reused, not built for this work.
The complete checked-source runner passed **399 tests** in **559.902 seconds**
(including discovery and report preparation). The unittest execution itself took
559.467 seconds.

All **14 CPU profiles / 20 probe suites passed** on these preserved bundles:

| Profile | Probe suites | Passing workload or boundary |
| --- | --- | --- |
| UART | 4 | 11 bootstrap cases, request/reply, liveness and seven Raw cases; 128 request/reply and 128 Raw exchanges |
| Screen | 3 | 14 normal/authority cases, two emergency-UART panic cases and two forced shared-IRQ boundaries |
| Services | 2 | 12 normal/fault/authority/DMA cases and maximal-copy device latency |
| UART stress | 1 | Four compiled clients, 512 writes, 12,928 hardware timer IRQs, FIFO output and reaping |
| Screen stress | 1 | Four compiled clients, 512 writes, 13,559 hardware timer IRQs, FIFO replies and no retained DMA pin |
| Memory | 1 | 20 heap/IPC lifetimes, loader publication, exhaustion recovery and timer preemption |
| Sharing | 1 | Owner-first and borrower-first death, revoked mappings and safe frame reuse |
| Objects | 1 | 20 Raw/20 Service lifetimes, 41 consent transfers, 640 denied unsolicited copies, quota recovery and stale tickets |
| Supervisor | 1 | 25 child lifetimes with fault/termination, collection and reuse |
| Recovery | 1 | Five generations, eight actual faults, five explicit reconnects/reads and zero final live children |
| Recovery production | 1 | Ordinary five-task graph, successful read and bounded ongoing-service observation at window 512/96 |
| Latency | 1 | Four seven-client FIFO rounds, maximum-copy/map/reap sections and generation boundary |
| HID | 1 | Five clients, 40 requests, 32 retained/32 dropped events, one overflow indication and 809 hardware timer IRQs |
| Media | 1 | Actual in-flight floppy removal and same-capacity replacement, client failure and peer survival |

The Latency profile also gained a second suite after this campaign (the same probe
at 4 × 32M = 128 MiB, `ram128m`), so it now has two suites. The Loader profile (`loader`, the 16th) was also added after it: Input, Disk and Files read a child's ELF from the storage volume and `SYS_TASK_LOAD` runs it, with tampered volumes refused (see [Files-backed loading](../docs/FILES_LOADER.md)). The Soak profile was added after this campaign and is not part of the table or
the checked-in provenance. It boots `LAIX_CONSOLE=soak` and runs 4096 child
lifetimes (512 faults) under one user supervisor, which checks every round's
`SYS_LIFETIME` report and completion event; `probe_soak_cpu.py` then checks the
final kernel state (slot 2 at generation 4095, other slots unused, no live or
leaked resources, last 32 history events). A local run (about 80 s) and a local
`build_acceptance_bundle.sh` / `acceptance_bundle.py run` replay passed on the
changed tree; no remote run and no new provenance record exist yet.

The [checked-in provenance](ACCEPTANCE_CI_PROVENANCE.json) contains each suite's
command, result, input hashes and the hashes/paths of all preserved reports and
raw logs. The reused emulator SHA-256 is
`3425ae5ec6000cdfcb9356374658a790435663765c6159d9f1c7caba89e1c90d`;
the reused ROM SHA-256 is
`cab72f0aa602b35f746555c05289be61cd8271bba11ee4d773c7a625230c9965`.
These are local macOS tool bytes; remote Ubuntu jobs require their separately
approved Linux emulator artifact. A remote GitHub Actions run was not performed.

A git revision alone is insufficient for a dirty worktree, so content manifests
are authoritative. Recording current WRM/firmware sources does not certify that
approved tool bytes were compiled from those sources.

The maintained bootstrap denial case calls the current `SYS_TASK_CREATE` with
a valid image and verifies `EPERM` for a client lacking factory authority. The
older unsupported-syscall expectation applies only to the historical image.

Raw CPU acceptance covers both arrival orders, message snapshotting, 32-byte
capacity and 33-byte rejection, small-receive retry/FIFO, rights/invalid buffers,
scoped cancellation on both wait sides, actual owner fault and final endpoint
reclamation. Its stress workload uses 128 maximal exchanges under real timer
rotation with conserved references and unique ready/wait membership.

UART and Screen stress use four separately instantiated **compiled M** clients,
each performing 128 complete writes and one-second hardware sleeps. The probe
checks request/reply FIFO identity, successful complete responses, timer IRQs,
client reaping and idle service waits. UART additionally compares exact emitted
bytes with completion order; the separate natural Screen probe checks pixels
against the preserved disk font. The stress claim is bounded to this workload.

DMA death observes the physical BUSY flag at the faulting fetch, at `taskFinish`
and immediately after the watchpoint on the actual `TASK_DEAD` store. The timeout
fixture changes a snapshot's physical disk delay to six seconds; the unchanged
compiled Disk service times out its five-second IRQ wait and cancels while BUSY.
Pins survive cancellation until natural device completion. Canaries are checked
at release and again after two real timer IRQs, including a marker in the freed
page. No device MMIO, instruction or PTE is patched by these probes.

Shared video IRQ fixtures set real DONE and VBLANK state through the existing
snapshot interface. Rearm rejects the held level, then compiled Screen code
acknowledges both causes and retries. The wait fixture selects an armed grant
under EXL, supplies the shared level at wait publication, observes the actual
blocked TCB and hardware IRQ, and verifies that the held level remains masked.
Grant owner/generation, executable instructions and mappings remain identified.
These are forced CPU/device fixtures, rather than natural host timing claims.

Media fixtures use a dedicated compact resource image. Snapshot loading invokes
the existing removable-drive `disk_eject`/`disk_insert` API while its real DMA is
BUSY, including a different medium with the same capacity. The original storage
authority becomes unusable, the client fails, pins are released and Input survives.
This does not certify Screen swaps between bitmap halves or cache-hit validation.

HID uses the existing host input script, not writes to the keyboard FIFO or
fabricated IPC replies. After a deterministic boot calibration, 64 down/up
HID events arrive at one tick. The real FIFO retains 32 and drops 32; compiled
Input/broker code distributes the ordered retained events across five clients
and reports overflow once. Forty requests and eight seconds of timer progress
complete with reaped clients. Physical SDL keyboard routing is a separate UI path.

Explicit runtime Disk/Files replacement is implemented and accepted by Recovery's
five-generation campaign. Fixed UART/Screen boot restart (the supervised Screen of G3 is not a CI profile), physical stuck
engine/reset acceptance, persistent storage, network services and universal
latency guarantees remain outside these contracts. Historical test counts and
hashes in older reports are unchanged and do not certify these new bundles.

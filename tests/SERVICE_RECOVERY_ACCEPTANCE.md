# A5 service recovery acceptance

Date: 2026-10-05. Scope: private user supervision, fresh identities, explicit
resolution, immutable Files/Disk dependency recovery and quiescent device
handover. WRM and firmware were **not built**; CPU runs use the existing
`bin/wrm081632` and `bin/firmware.rom` bytes. Only LA/IX images were compiled.

The [contract](../docs/SERVICE_RECOVERY.md),
[checked-source tests](test_service_recovery.py),
[user-policy tests](test_recovery_policy.py),
[CPU probe](probe_service_recovery_cpu.py) and
[provenance record](SERVICE_RECOVERY_PROVENANCE.json) establish the evidence
levels below. A checked audit box means the stated evidence, not a claim that
all fault-injection cases were run on hardware.

## Implementation evidence

| Requirement | Implementation and verification |
| --- | --- |
| User launch/failure/retry policy | `user/recovery/policy.m` and `supervisor.m`: scoped TaskEvent polling, timed client reports, consumer-first retirement, producer-first launch, five attempts, 1/2/4/8-second backoff; `PolicyTests` executes checked policy with syscall-result fixtures |
| Separate instance/protocol/resource/name identities | `RecoveryStart`, `ServiceResolution`, unchanged version-1 headers, monotonic publication generations and full task references; generation/dependency/exhaustion tests |
| Fresh task, endpoint, startup and grants | Runtime immutable ELF catalog, existing task/memory/endpoint factories, new Disk preflight/regrant; 24-cycle source test and five-generation CPU case |
| Transactional publication | `servicePublish` checks row capacity/context/dependency/generation before making a Created child runnable, then commits discovery under IRQ exclusion; atomic publication capacity/context test and construction fault injection |
| Authorized consenting resolver | Nontransferable per-child owner/name enrollment; resolve installs SEND in caller only, rolls back invalid destination and returns EMFILE for a full caller table; authorization/mask/rollback/table tests |
| Old handles remain revoked | Manager death plus existing endpoint generations; no rebinding API; source stale task/endpoint/reply/IRQ test and CPU retained-handle checks |
| Reconnect and blocked-client notification | EPIPE/deadline completes the old call; client closes and explicitly resolves after sleeping/polling; CPU client performs five explicit reconnects |
| Files/Disk dependency recovery | Current chain is cancelled; retire Files before Disk, launch fresh Disk before fresh Files; same resource generation and fresh dependency send handle; checked policy order test, dependency publication rejection and real CPU Disk fault inside Files read |
| Restored/lost state and duplicate policy | Contract specifies immutable data reconstruction, lost pending replies, idempotent read/stat/echo retry; writes/non-idempotent replay unsupported, requiring future IDs/duplicate suppression |
| Bounded attempts and backoff | Source policy test checks limit/exhaustion and sleep sequence; CPU runs natural timer preemption and backoff across four recoveries |
| Quiescent IRQ/device handover and terminal quarantine | `diskDevicesCheck` before `irqIssue`; BUSY pin/owner retained, no CHANGED acknowledgement, fresh grant only after quiescence; BUSY/regrant/changed-medium source tests and five physical IRQ renewals on CPU |
| Replacement wire resource generations | Checked Disk/Files handlers use startup generation; CPU rejects old generations on fresh endpoints without killing the server |

## Acceptance evidence

| Audit acceptance item | Result and boundary |
| --- | --- |
| Repeated stateless crashes without live-resource growth | **Source:** 24 replacements, exact free-page baseline after each reap, bounded control counts and zero final endpoint references. **CPU:** four Echo faults followed by a fifth live generation, successful calls, zero final live runtime children/endpoints/controls/IRQs/DMA buffers |
| Old task/endpoint/IRQ/reply references cannot reach replacements | **Source:** retains and exercises all four token classes after reuse, rejects late replies and old task controls, checks fresh IRQ token and one cancellation wake. **CPU:** retains old client handles until fresh resolution, verifies EPIPE and new instance/resource generation; five IRQ generations |
| Explicit reconnect and successful requests | **CPU:** five consenting resolutions of Echo and Files, successful Echo reply and 16-byte file read each generation; no kernel instruction or user instruction patching |
| Disk dies during Files call, then later read succeeds | **CPU:** Disk's fixture faults after accepting the read at offset 32, while Files owns the upstream reply wait; Files returns EPIPE/exits, client completes, user supervisor replaces the dependency chain; next generation reads offset 0 successfully |
| BUSY prevents replacement submission/regrant | **Source:** trusted hardware fixture holds BUSY with a retained DMA pin across owner death and repeated scheduling/reaping; replacement factory returns EBUSY and grants no authority; late completion releases pin, then fresh IRQ/device grant succeeds |
| Indefinite BUSY reports unavailable while unrelated work progresses | **Source:** BUSY is held through repeated yields with no release/regrant; resolver withdrawal returns EPIPE. **Policy fixture:** five bounded sleeps, unavailable/quarantined result, retained completion capability and no collect/free/new grant. **CPU:** unrelated peer survives all crash cycles; indefinite physical BUSY is not injected on CPU |
| Every construction/publication step fails closed | **Source:** inject every actual ELF catalog allocation/mapping acquisition, first/second handle installation, startup allocation/map, invalid publication context/generation, registry capacity and stale dependency; exact page/control rollback and no partial advertisement. These are source/ledger fault injections, not CPU instruction patches |
| CPU recovery with source/image provenance | **CPU:** natural user execution over five generations, eight real misaligned-load faults, five successful reads, timer preemption, four real backoff sleeps and an unrelated peer; artifact/source SHA-256 manifest checked before and after each run |

The legacy static `services` profile was also rebuilt and passed its existing
natural-boot/read CPU regression after the shared loader and entry changes.

The separate production profile smoke test boots the normal server/client/
supervisor images (without crash hooks), observes a successful 16-byte read
before a subsequent request, and verifies all five tasks are still live with
no completion/fault records. It is a smoke test, not production fault injection.

## Recorded results

The complete fresh source suite passed: **357 tests in 506.905 seconds**.
The final recovery CPU run passed five generations, eight real faults and five
successful reconnect/read cycles. The normal profile CPU smoke and the legacy
`services` natural-boot/read regression both passed. All runs used the existing
WRM/ROM hashes recorded in the provenance file; neither was built or modified.

## Reproduction

Run checked-source regression without producing code:

```sh
python3 -B -m unittest discover -s laix/tests -p 'test_*.py'
```

Run the normal profile smoke probe against matching freshly built LA/IX bytes:

```sh
LAIX_CONSOLE=recovery sh laix/build.sh
python3 -B laix/tests/probe_service_recovery_cpu.py \
  laix/build/recovery.img laix/build/recovery.map --production
```

Run dedicated CPU crash acceptance:

```sh
LAIX_RECOVERY_FIXTURES=1 LAIX_CONSOLE=recovery sh laix/build.sh
python3 -B laix/tests/probe_service_recovery_cpu.py \
  laix/build/recovery.img laix/build/recovery.map
```

`LAIX_RECOVERY_FIXTURES=1` selects only test images in `tests/programs/recovery`.
Production server code has no crash command/fault hook. The CPU probe verifies
that its requested mode matches the stamped build and rejects source/artifact
mismatch. Logs and complete manifests are in
`build/acceptance/service-recovery/{results,production}.json` and accompanying
UART/monitor text files. The checked-in provenance record preserves these
results and hashes of their logs even if a later build replaces local images.

## Fixture boundaries and remaining limitations

Source evaluators execute checked M logic with synthetic physical/linker
addresses and fixture hardware. Syscall/IRQ scheduling transitions use the real
checked kernel AST. Mutable scalar startup initializers are now honored by
`SourceM`, including the legacy generation-1 default; CPU validation separately
checks the emitted data. The user-policy fixture models syscall results and
clock sleeps to test bounds/order; it does not replace kernel or CPU checks.

CPU crash runs use actual embedded test services, syscalls, protected startup,
real timer interrupts, Disk reads and real faults. Monitor operations set
breakpoints and read state; they do not alter instructions, task contexts,
authority ledgers or DMA status. The Disk crash fixture faults before submitting
its selected failing DMA read. Physical BUSY/late completion/indefinite BUSY
proofs are source/hardware fixtures, explicitly not CPU injections. CPU final
cleanup and source per-cycle accounting provide different evidence levels.

The first contract covers stateless Echo and immutable boot Files/Disk, not
persistent writes, arbitrary devices, media replacement, hot namespace
migration or automatic client-process restart. General capability transfer
(A6), a general device interface (A7), writable transaction recovery and
hardware DMA abort remain separate work. Screen/font retain their boot-only
policy. Indefinite BUSY deliberately leaves unavailable/quarantined resources
until reset; no safe successful recovery is claimed for that case.

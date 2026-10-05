# A6 capability transfer and domain quota acceptance

Date: 2026-10-05. [A6 checklist](../docs/AUDIT_06_CAPABILITY_TRANSFER_QUOTAS.md).
[Normative contract](../docs/CAPABILITY_TRANSFER.md).

All implementation and acceptance requirements are covered at the evidence
levels specified here. Checked-source execution and CPU execution are distinct;
a checked requirement does not imply every adversarial branch was run on CPU.
WRM was not built. The LA/IX `objects` image was compiled with the existing MC
toolchain and executed with the existing WRM binary and firmware ROM.

## Requirement mapping

The named tests below are in [test_capability_transfer.py](test_capability_transfer.py)
unless another file is linked explicitly.

| Implementation requirement | Implementation and evidence |
| --- | --- |
| Receiver-controlled mechanism | Selected free slot plus sender/lifetime/exact-rights ticket; `transferReserve`; `test_exact_slot_rights_and_authenticated_atomic_notification` |
| Remove arbitrary task-ID installation | `ipcCopy` self-table check; `test_sixteen_unsolicited_raw_and_service_copies_leave_foreign_budget_unchanged`; CPU rejects 640 attempts |
| Define transferable rights | Contract rights table; `handleCopyCheck`; exact-rights and revoked/insufficient-source tests; existing Service receiver binding tests |
| Couple authority to authenticated delivery or separate atomic notification | `transferCommit` publishes installed handle and kernel-authenticated sender/rights atomically; `transferCollect` registers; exact-notification source test and CPU user helper |
| Reservation lifetime, expiry, cancellation, staleness | Persistent ticket generations, full peer references, 1–60 second hardware deadline; forged/cancelled, expiry-without-waiters and task-reuse/retirement tests; CPU cancel/expiry |
| Validate attenuation/liveness/consent before charging | Shared `handleCopyCheck`, then selected-slot install; exact-rights and failed-source tests conserve endpoint references |
| Multi-capability transaction policy | One handle per ticket; batches are not exposed. Contract explicitly defines separate commits as separate transactions; consumed-ticket test and CPU replay rejection |
| Domain budgets including pins and queues | Contract accounting table; four ordinary child controls including completions; existing endpoint/frame quotas; grant offers charge lender, borrower explicitly maps; task-quota and memory-offer tests |
| Supervisory reserve | Existing two endpoints/two supervisor handle slots and sixteen trusted frames; new final two task slots/control rows; task-quota test, existing object/frame reserve tests, CPU peer-table exhaustion and supervisor allocation/cleanup |
| Release/revocation at death | `transferReleaseTask`, existing object lifecycle and grant teardown; sender/receiver death matrix, receiver lifetime reuse and memory offer teardown; CPU sender death after commit and zero final ledgers |
| Permanent sixteen-copy regression | Raw and Service checked-source regression; sixteen attempts in each of forty CPU endpoint lifetimes |
| Attenuation versus resource interference | Contract introduction explains the distinction and the foreign-table restriction |

| Acceptance requirement | Evidence level and result |
| --- | --- |
| Sixteen unsolicited copies do not change recipient table/budget | Checked-source: memory snapshot unchanged for Raw and Service; foreign allocation still succeeds. CPU: 640 denied copies; peer restores its own exhausted table. |
| Accepted selected slot, exact rights and authenticated notification | Checked-source: Raw SEND/RECEIVE/both and Service SEND; exactly one reference increment, chosen slot 5, exact sender/mask. CPU: forty selected-slot transfers plus final death-boundary transfer, wrapper verifies slot 2/sender/rights. |
| Forged, stale, consumed, expired permissions rejected | Checked-source: zero/high-bit/wrong-generation/wrong-slot/wrong-caller tickets, cancelled/replayed tickets, task-slot reuse and retirement. CPU: forged generation, exact-mask mismatch, replay, cancellation and expiry. |
| Full tables, dead recipients, revocation conserve resources | Checked-source: full/retired/busy reservation failures, source close/revocation/insufficient rights, dead and reused recipient, no new references, reservation retry/cancel. CPU: full peer table rejects copy/reservation and is restored. |
| Exhausted client does not stop peer/supervisor budgets | Checked-source: ordinary task quota includes four uncollected completions; supervisor still allocates, collection refunds quota, ordinary task slots fill and supervisor uses slots 7/8. Memory offers cannot exhaust borrower grant quota. Existing endpoint and memory regressions independently verify other domain allocations and reserves. CPU: peer exhausts sixteen handles while supervisor proceeds through endpoint quota recovery, child/device allocation and completion. |
| Sender/receiver death at transfer boundaries | Checked-source: both deaths before/after commit clear reservations and references; receiver reuse rejects old task/ticket; lender death drops unused offer pins. CPU: sender exits after commit, receiver collects authenticated notification, observes EPIPE, closes stale handle and exits; monitor verifies all references/records/authority are zero. |
| Source regressions followed by CPU consent/exhaustion | Focused source suites passed before the CPU probe; final full-suite result below. CPU probe passed on newly built LA/IX image with existing WRM/ROM. |

The linked existing suites also cover rollback after every task allocation/map
failure, destroyed-but-pinned endpoint accounting, queued/accepted IPC pins,
DMA quiescence, memory owner/borrower teardown and physical-frame refunds:
[test_runtime_tasks.py](test_runtime_tasks.py),
[test_runtime_objects.py](test_runtime_objects.py),
[test_runtime_memory.py](test_runtime_memory.py),
[test_memory_sharing.py](test_memory_sharing.py),
[test_ipc_liveness.py](test_ipc_liveness.py).

## CPU observation and reproducibility

[runtime_objects_user.m](programs/ipc/runtime_objects_user.m) now uses explicit
reserve/commit/collect handshakes instead of cross-task `copyHandle`.
[probe_runtime_objects_cpu.py](probe_runtime_objects_cpu.py) waits for three
user exits before final idle (expiry tests legitimately idle earlier). The
monitor checks both fixture users exited with code zero and were reaped, runtime
children are Empty, all endpoints have zero references/lifetime owners/waiters,
all task handles/factory roots are gone, every transfer state/peer/slot/rights/
handle field is zero, task controls/ready queue are empty, device grants are
released and UART contains no panic. Real `taskTick` proves timer preemption.

Commands, with no WRM source build:

```sh
PYTHONPATH=laix/tests python3 -m unittest test_capability_transfer test_ipc_handles test_runtime_objects test_runtime_tasks test_memory_sharing
python3 -m unittest discover -s laix/tests -p 'test_*.py'
LAIX_CONSOLE=objects sh laix/build.sh
python3 laix/tests/probe_runtime_objects_cpu.py laix/build/objects.img laix/build/objects.map --log-dir laix/build/acceptance/capability-transfer
LAIX_CONSOLE=supervisor sh laix/build.sh
python3 laix/tests/probe_runtime_tasks_cpu.py laix/build/supervisor.img laix/build/supervisor.map --log-dir laix/build/acceptance/capability-transfer/supervisor
python3 laix/tools/capability_transfer_provenance.py
```

CPU logs, reports and final source-test output are generated under
`build/acceptance/capability-transfer/`. The checked-in
[provenance manifest](CAPABILITY_TRANSFER_PROVENANCE.json) records exact source,
compiler, image/map, WRM/ROM and acceptance-output SHA-256 values. Base commits
alone do not identify the modified working-tree inputs.

## Final verification

The final complete source suite passed **369 tests in 533.229 seconds**.
All twelve A6 source regressions passed, including the exact SEND-only sixteen-copy
reproduction (also rechecked independently after strengthening its fixture).
The objects CPU regression and supervisor CPU regression both passed on the
final LA/IX images. Shell/Python syntax checks and `git diff --check` passed.
The objects CPU probe also explicitly checks every handle reservation byte is
zero at final teardown. Full source output is saved as `source-tests.txt` beside
the CPU reports; focused and build logs are saved there as well.

## Limits of evidence and behavior

This is an endpoint-only, one-slot, one-ticket-at-a-time protocol with a separate
kernel notification; ordinary IPC payloads have no capability attachment ABI.
Source tests cover receiver death/reuse, Service RECEIVE restrictions, task
reserve pressure, memory-offer consent/accounting, source revocation and boundary
generation retirement; these are not all claimed as CPU adversarial cases.
CPU directly covers Raw/Service SEND delivery, Raw RECEIVE delivery, consent,
unsolicited exhaustion, ticket forgery/replay/cancel/expiry, local handle
exhaustion, quota recovery, sender death after commit and final reclamation.

Task/memory/device/factory authority remains nontransferable. Grant offers pin
the lender's existing frames and are accepted by an explicit borrower mapping;
no grant retransfers ownership. A stale installed handle remains charged until
closed. There is no delegation-tree revocation, group-domain quota system or
multi-capability transaction. Recovery reserves protect ordinary runtime
pressure; trusted boot/privileged supervisors can still overcommit them, and
retired generations or indefinitely quarantined hardware can reduce capacity.

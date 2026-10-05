# Runtime object and resource creation acceptance

Date: 2026-10-05. Scope: A3's first scoped runtime, using separate checked
endpoint, task-control, memory and narrow UART/input/IRQ interfaces.

The twelve implementation and six acceptance requirements in
[A3](../docs/AUDIT_03_RUNTIME_CAPABILITIES.md) are complete at the evidence levels
below. [Runtime objects](../docs/RUNTIME_OBJECTS.md) is the normative contract.
No WRM source build was performed. LA/IX images were built with the existing
MC toolchain and executed with the existing WRM binary and firmware ROM.

## Implementation requirement mapping

| A3 requirement | Implementation / evidence |
| --- | --- |
| Object inventory and operations | Runtime contract's object inventory; existing [task acceptance](RUNTIME_TASKS_ACCEPTANCE.md) and [memory acceptance](RUNTIME_MEMORY_ACCEPTANCE.md), plus new endpoint/device tests below |
| Typed objects or separate interfaces; common authorization | Separate endpoint, task, space/region/grant and IRQ/broker ledgers; caller identity, owner/control, rights, generation and IRQ exclusion documented in the runtime contract |
| Factory rights and quotas distinct from object rights | `HandleTable.factoryModes/factoryQuota/factoryRecovery`, `Task.createImages/deviceFactory`; `test_factory_root_validation_and_mode_attenuation` and quota/reserve tests |
| Delegate bounded creation to supervisor | `supervisorBootstrap` grants modes 0/1, quota 12, recovery access and UART/input mask; the supervisor CPU regression runs this boot policy |
| Runtime checks with sealed bootstrap roots | `ipcCreate`, `endpointFactoryCreate`, `taskRuntimeDevices`, `irqIssue`; tests deny post-seal endpoint/factory/IRQ bootstrap issuance |
| Service creation and immutable receiver | `Endpoint.manager` captures a full task reference; creator-held CONFIGURE selects an unconfigured Created child; no rebind operation; foreign-Service test |
| Attenuation and no numeric authority | `handleCopy` checks source rights, creator and receiver; task control checks foreign child references; runtime authority and foreign-Service tests |
| Destroy versus close; generations | `endpointDestroy`, `handleClose`, both generation checks and retirement; cancellation and boundary tests |
| Creator versus designated receiver | `Endpoint.creator` owns management/charge; `manager` owns Service receive/reply; foreign-Service and both-owner death tests |
| Exhaustion, installation failure and rollback | Object/quota/handle errors, zero-reference object release, inherited rights/startup rollback; quota, reserves, root-install and construction-fault tests |
| Defined object/wait budget and recovery capacity | 16 fixed endpoint/queue slots, one fixed wait/snapshot/reply record per TCB, 16 handles/task, two recovery endpoint and supervisory handle slots; reserve test and runtime storage contract |
| Scope of revocation | Whole-object destruction and one-reference close; no delegation-tree revocation; explicit runtime revocation contract |

## Checked-source adversarial acceptance

[tests/test_runtime_objects.py](test_runtime_objects.py) contains eleven tests
that execute the checked M kernel sources and real `userSyscall` dispatcher in
the existing interpreter/device fixtures. All factory grants are established
before `taskStart` seals bootstrap issuance. Tests do not forge runtime
creation authority after the seal.

| Acceptance requirement | Tests and observed behavior |
| --- | --- |
| New Raw and Service endpoints after scheduling starts | `test_post_start_raw_exchange_destroy_and_authority_denial` transfers Raw data and revokes peer handles. `test_service_binding_to_controlled_child_and_split_ownership` configures/publishes a fresh receiver, exchanges call/reply, rejects creator accept and receiver destroy, and revokes on last usable receiver close. |
| Deny unauthorized creation and amplification | Raw exchange test rejects an unprivileged factory and amplification; `test_factory_root_validation_and_mode_attenuation` rejects malformed modes, duplicate/invalid root grants and unsupported modes. Foreign-Service test denies receive copies to unrelated tasks and access through a bare foreign task reference. Device test checks masks and foreign child control. |
| Destroy queued and accepted calls exactly once | `test_queued_and_accepted_calls_cancel_exactly_once` creates a Service endpoint at runtime, accepts one client and leaves another queued. Destroy gives both `-EPIPE` and exactly one wake, removes exactly two wait pins, rejects the old reply token and duplicate destroy, then releases all handles to zero references. |
| Recover from object and handle exhaustion; supervisor progress | `test_quota_counts_destroyed_references_and_recovers` fills quota 12, charges a Destroyed-but-pinned object, closes it and creates a replacement while scheduling and child creation continue. `test_object_and_handle_reserves_allow_supervisor_recovery` fills all 14 ordinary object slots, permits two recovery roots, fills ordinary supervisor handles to 14, then installs a recovered factory root in slot 15 after releasing an ordinary object. |
| Reuse rejects stale handles; boundary generations retire | Quota test reuses a slot and denies the old handle. `test_boundary_generations_retire_and_stale_handles_never_rebind` issues the last legal handle and endpoint generations, closes them, observes endpoint retirement and allocation in different slots without wrapping. Existing task tests also preserve handle generations across task reuse. |
| Construction/installation faults leave no unowned object or hidden root | `test_faulted_installation_and_full_table_leave_no_hidden_root` injects root installation failure after object initialization, and separately retires every remaining handle slot. Both restore ownership/reference charges; retry consumes a fresh object generation. `test_service_construction_faults_revoke_unpublished_receivers` fails inherited handle installation, startup frame allocation and startup mapping: installation is retryable, later failures discard/reclaim the child and revoke its Service endpoint, and closing the creator root clears ownership. |

`test_creator_and_receiver_death_revoke_with_different_owners` separately tests
both lifetime owners and creator factory revocation.
`test_device_factory_checks_control_masks_and_irq_reissue` creates exclusive
INPUT grants, denies duplicate/foreign/unsupported issuance, discards the
original child, issues a new generation and rejects the old token against the
new owner. Existing IRQ tests cover bounded waits, masking, generation checks,
timeouts and death; the new factory does not expose an arbitrary-line syscall.

## CPU acceptance

The dedicated `objects` image boots two user programs with only a control
channel, then creates its private endpoints after normal user scheduling
starts. [runtime_objects_user.m](programs/ipc/runtime_objects_user.m) performs
20 Raw and 20 Service lifetimes with real syscall/trap/MMU paths. Each lifetime
copies attenuated peer rights, exchanges payloads, closes the peer reference,
destroys and closes the root, and rejects a stale root on the next allocation.
The peer denies unauthorized creation and attempts to amplify each copied
handle. A real timer interrupt reaches `taskTick` during the run.

The supervisor fills its quota (eleven runtime objects plus its persistent boot
control channel), observes `-ENFILE`, releases all roots and successfully
creates another. It creates/discards two unpublished children with exclusive
keyboard INPUT grants, proving IRQ generation renewal, then grants UART TX to
another child, configures/publishes it and collects its normal completion.

[probe_runtime_objects_cpu.py](probe_runtime_objects_cpu.py) passed and
verified both fixture users exited with code zero and were reaped, runtime
child slots are Empty, every endpoint reference/creator/receiver/wait count is
zero, all task-local handles and factory roots were removed, the keyboard grant
and input broker have no owner, grant generation is two, control ledgers and
ready queue are empty, and UART contains no panic. It verifies source-derived
TCB layout against the map and input artifacts remain byte-identical.
The monitor cannot dump PIC MMIO; IRQ masking is verified by checked-source
IRQ/device fixtures rather than claimed as a CPU register observation.

The existing `supervisor` CPU regression also passed on a newly built image:
24 normal child exits, one alignment fault, final supervisor exit, generation
reuse, completion collection and physical resource reclamation.

Logs are generated under `build/acceptance/runtime-objects/`: `results.json`,
`objects.uart.txt`, `objects.monitor.txt`, and `supervisor/` regression logs.

## Final verification

The full LA/IX regression passed **331 tests in 392.355 seconds**, including
all eleven new runtime object tests. The three focused context-alignment and
syscall-contract rechecks also passed. Both CPU probes passed on the final
images; shell syntax, Python syntax and `git diff --check` passed. The final
source-suite output is generated at
`build/acceptance/runtime-objects/source-tests.txt`.

## Provenance and remaining limits

[Source and artifact manifest](RUNTIME_OBJECTS_PROVENANCE.json) records every
LA/IX source/assembly input, user image fixture, font input, build/probe script,
MC toolchain source and artifact SHA-256. It identifies the WRM, LA/IX and MC
base commits; working-tree hashes, rather than the LA/IX base commit alone,
identify the changed sources used for this acceptance.

CPU acceptance covers every new syscall, 40 complete endpoint lifetimes,
attenuation denial, quota recovery, stale handles, real timer preemption,
keyboard grant renewal, UART issuance and final reclamation. Foreign Service
receiver binding, queued/accepted destroy cancellation, construction faults,
reserved-slot pressure and boundary generations use checked-source evidence;
they are not claimed as additional CPU adversarial scenarios.

The first runtime supports only catalog image 1 and UART TX/keyboard INPUT
resource factories. Disk DMA and screen/font regrant remain A5/A7 work.
The subsequent [A6 completion record](CAPABILITY_TRANSFER_ACCEPTANCE.md) supersedes
the original unrestricted ordinary copy path with receiver-controlled delivery.
The provenance and CPU results above describe this earlier A3 milestone. There is no
endpoint receiver rebinding, general task-control/factory transfer, arbitrary
IRQ/MMIO issuance or delegation-tree revocation. Trusted boot must leave its
recovery reserves available; retired generations permanently reduce capacity.

Reproduce using existing WRM/ROM bytes:

```sh
python3 -m unittest discover -s laix/tests -p 'test_*.py'
LAIX_CONSOLE=objects sh laix/build.sh
python3 laix/tests/probe_runtime_objects_cpu.py laix/build/objects.img laix/build/objects.map
LAIX_CONSOLE=supervisor sh laix/build.sh
python3 laix/tests/probe_runtime_tasks_cpu.py laix/build/supervisor.img laix/build/supervisor.map --log-dir laix/build/acceptance/runtime-objects/supervisor
```

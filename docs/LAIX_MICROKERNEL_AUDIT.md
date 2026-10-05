# LA/IX microkernel completeness audit

Date: 2026-10-04.

LA/IX already has the essential mechanisms of a small, statically configured
microkernel: isolated user tasks, preemption, protected IPC and user services.
The main gap is the ability to manage those mechanisms after boot. Task,
endpoint, memory and device authority are issued by embedded kernel init and
then sealed. A failed service can be contained, but cannot be replaced.

This audit uses a practical target: a single-CPU microkernel that can run
independent applications, create and retire their resources at runtime,
contain failures and recover services under explicit authority. A fixed
embedded system can reasonably stop earlier. POSIX, SMP, demand paging and a
full filesystem are separate requirements, not prerequisites for the
microkernel designation.

## Scope and evidence

The review covers the current working tree, including uncommitted LA/IX
changes, rather than only the recorded submodule revision. Reference commits
are WRM `aee696d` and LA/IX `ddbda97`; those hashes alone do not identify the
reviewed sources. The existing working-tree changes were left intact.

Reviewed code includes task construction/scheduling/cleanup, trap entry and
syscall dispatch, physical memory and MMU, endpoint/handle lifecycle, Raw and
Service IPC, IRQ grants, device brokers, all three bootstrap profiles and the
user console/screen/input/disk/file paths. Existing source tests, acceptance
reports and the root CI workflow were also inspected.

No WRM, firmware or LA/IX build was performed. Historical CPU acceptance is
cited as historical evidence; CPU probes were not rerun for this audit.
The fresh source-suite result and a focused reproduction are recorded below.
This is an architecture and readiness audit, not a proof that every code path
is correct or a comprehensive vulnerability assessment.

## What is already present

| Area | Current implementation | Evidence |
| --- | --- | --- |
| Privilege boundary | User trap entry switches to a trusted guarded kernel stack; context includes GPRs, FCSR and PTBR; IRET restores the selected task | [trap entry](../laix/src/trap/trap.asm), [user acceptance](../laix/tests/USER_ACCEPTANCE.md) |
| Address spaces | Separate directories, allocator ownership/reference tracking, private user pages, protected kernel mappings, NX stacks and W^X across aliases | [MMU](../laix/src/mm/mmu.m), [allocator](../laix/src/mm/memory.m), [MMU acceptance](../laix/tests/MMU_ACCEPTANCE.md) |
| User copying | Entire ranges checked before copying; translation uses the target task's directory and supervisor aliases; no partial writes on invalid ranges | [copy helpers](../laix/src/mm/mmu.m), [buffer acceptance](../laix/tests/USER_BUFFERS_ACCEPTANCE.md) |
| Scheduling | Eight user task slots, one execution context per task, FIFO round-robin, timer preemption, yield, guarded idle and deferred reaping | [tasks](../laix/src/task/task.m), [scheduler acceptance](../laix/tests/SCHEDULER_ACCEPTANCE.md) |
| Endpoint capabilities | Per-task tables, attenuated send/receive/manage rights, object/handle generations, bounded references, close/destroy and owner-death revocation | [objects](../laix/src/ipc/objects.m), [capability contract](../laix/docs/05_IPC_RIGHTS.md) |
| IPC | Raw send/receive and atomic Service call/accept/reply, 32-byte snapshots, FIFO admission, owner-bound one-use reply rights and cancellation on death/revocation | [IPC](../laix/src/ipc/ipc.m), [request/reply acceptance](../laix/tests/IPC_REQUEST_REPLY_ACCEPTANCE.md) |
| User services | UART server/application; optional screen/bitmap profile; separate input/disk/files/application profile with limited ELF boot loading | [stage 6](../laix/docs/06_USER_SERVICES.md), [simple services](../laix/docs/SIMPLE_SERVICES.md) |
| IRQ and DMA isolation | Generation-bearing IRQ grants, masked level notifications, atomic wait/rearm, timed IRQ waits, checked physical bounce-buffer DMA and quarantine until quiescence | [IRQ](../laix/src/drivers/irq.m), [broker](../laix/src/drivers/service_devices.m), [safety acceptance](../laix/tests/DEVICE_SAFETY_ACCEPTANCE.md) |
| Failure containment | User faults terminate the task; queued and accepted service callers get EPIPE; unrelated tasks keep running; emergency kernel UART does not depend on user services | [termination](../laix/src/task/task.m), [simple-service acceptance](../laix/tests/SIMPLE_SERVICES_ACCEPTANCE.md), [panic acceptance](../laix/tests/DEVICE_SAFETY_ACCEPTANCE.md) |

In particular, IPC, user-mode drivers and DMA pinning are not missing outright.
Their current restrictions and unfinished validation should be extended from
this baseline.

## Priorities

These priorities describe completeness, not CVE severity:

- **P1:** prevents a reusable system from managing applications and services.
- **P2:** needed for reliable operation, resource isolation or convincing acceptance.
- **P3:** useful extension or optimization after the basic lifecycle works.

| ID | Missing capability or limitation | Priority |
| --- | --- | --- |
| A1 | Runtime task lifecycle and a user-space supervisor | P1 |
| A2 | Authorized memory management beyond fixed startup mappings | P1 |
| A3 | Runtime object/resource creation under scoped capabilities | P1 |
| A4 | [Accepted](../tests/IPC_LIVENESS_ACCEPTANCE.md): timed calls, try IPC, scoped cancellation and watchdog sleep | P1 |
| A5 | [Accepted](../tests/SERVICE_RECOVERY_ACCEPTANCE.md): private supervision, atomic publication, explicit resolution and quiescent Disk handover | P1 |
| A6 | Receiver-controlled capability transfer and resource quotas | P2 |
| A7 | A general device interface with less service policy in the kernel | P2 |
| A8 | [Accepted](../tests/LIMITS_LATENCY_ACCEPTANCE.md): resource/lifetime budgets, reply retirement/replacement and measured CPU latency | P2 |
| A9 | CPU stress coverage, CI and consistent acceptance tracking | P2 |
| A10 | Optional shared memory, threads, pager and scheduler extensions | P3 |

## Detailed work items

Each item has its own implementation and acceptance checklists in `laix/docs/`.
All unchecked entries describe proposed work; the current baseline is listed
above. A10 is an optional feature-selection checklist.

- [A1: Runtime task lifecycle and supervision](../laix/docs/AUDIT_01_TASK_LIFECYCLE.md)
- [A2: Runtime memory authority](../laix/docs/AUDIT_02_RUNTIME_MEMORY.md)
- [A3: Runtime object and resource creation](../laix/docs/AUDIT_03_RUNTIME_CAPABILITIES.md)
- [A4: IPC liveness and deadlines](../laix/docs/AUDIT_04_IPC_LIVENESS.md)
- [A5: Service restart and discovery](../laix/docs/AUDIT_05_SERVICE_RECOVERY.md)
- [A6: Capability transfer can exhaust a foreign table](../laix/docs/AUDIT_06_CAPABILITY_TRANSFER_QUOTAS.md)
- [A7: Device mechanisms still contain service policy](../laix/docs/AUDIT_07_DEVICE_BOUNDARY.md)
- [A8: Limits and execution latency](../laix/docs/AUDIT_08_LIMITS_AND_LATENCY.md)
- [A9: Acceptance and CI need to follow the current architecture](../laix/docs/AUDIT_09_ACCEPTANCE_AND_CI.md)
- [A10: Extensions after the lifecycle baseline](../laix/docs/AUDIT_10_OPTIONAL_EXTENSIONS.md)

## Recommended implementation order

| Step | Deliverable | Completion condition |
| --- | --- | --- |
| 1 | Receiver-controlled handle transfer; IPC deadlines and scoped supervisor cancellation | Foreign tables cannot be filled unsolicited; stalled live servers release clients exactly once |
| 2 | Runtime resource model for tasks, memory and endpoints, with quotas and generation-bearing identities | Authorized code can construct, publish, terminate and reclaim resources while scheduling continues |
| 3 | User-space init/supervisor and a simple runtime loader | More than eight sequential application lifetimes in one boot; allocation failures roll back; foreign control fails |
| 4 | Explicit service restart/resolution and dependency recovery | Fresh capabilities reach reconnecting clients; stale tokens remain invalid; repeated crashes do not leak resources |
| 5 | Generalize the device broker and move service policy outward | Resource grants govern allowed operations; DMA safety survives owner death and late completion |
| 6 | Complete CPU stress and CI acceptance; then bulk IPC/performance extensions | Current artifacts meet documented limits and failure/recovery scenarios automatically |

Each step should add its own source and CPU acceptance rather than postponing
verification to step 6. A useful final scenario is an application calling a
file service, the disk service failing during an operation, safe cancellation
and DMA quarantine, supervised replacement when the device is quiescent,
explicit client reconnection and a successful subsequent read while an
unrelated application keeps running. This tests the missing lifecycle across
subsystems without requiring POSIX or a full filesystem.

## Checks performed for this audit

The complete existing source suite was run without code generation:

```sh
python3 -B -m unittest discover -s laix/tests -p 'test_*.py'
```

Result: **285 tests passed in 247.400 seconds**. These are fresh source checks,
not CPU acceptance of a newly built image.

The A6 reproduction used the existing `HandlesM` evaluator, which executes
the checked capability/IPC M source. It did not modify project files:

```sh
PYTHONPATH=laix/tests python3 -B - <<'PY'
from test_ipc_handles import HandlesM, SEND, error, MFILE
vm = HandlesM()
root = vm.bootstrap(1)
source = vm.call('ipcCopy', root, 2, SEND)
vm.select(2)
installed = [vm.call('ipcCopy', source, 3, SEND) for _ in range(16)]
assert all(0 < token <= 0x7fffffff for token in installed)
assert vm.call('ipcCopy', source, 3, SEND) == error(MFILE)
assert sum(vm.memory[vm.entry(3, slot, 'object')] != 0
           for slot in range(1, 17)) == 16
PY
```

The observed result was sixteen successful unsolicited installations followed
by EMFILE. This check establishes source behavior within the existing test
model; real CPU reproduction remains future validation.

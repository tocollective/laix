# Runtime task construction and supervision

The `supervisor` boot profile runs a user supervisor that creates 24 approved
children sequentially, collects their exit codes, then launches and collects
one faulting child. The kernel supplies mechanisms and an immutable image
catalog; launch order, arguments and completion handling belong to the user
image. Existing UART, screen and service bootstrap policies remain available.

```sh
LAIX_FIXTURE=supervisor sh laix/build.sh
LAIX_FIXTURE=supervisor sh laix/run.sh --headless --no-net
python3 laix/tests/probe_runtime_tasks_cpu.py laix/build/supervisor.img laix/build/supervisor.map
```

## Identity and authority

A task reference is a positive 32-bit word `(generation << 8) | slot`. Slot
numbers 1–8 are diagnostic indices. Generation zero is the first reservation,
maintaining the first-boot ABI; later reservations, including failed ones,
advance the generation. A slot whose generation reaches `0x7FFFFF` retires
instead of wrapping. Zero is never a task reference. `Task.id` now holds the
reference; `Task.slot` holds only the diagnostic index. `taskGet` checks the
whole reference and rejects Empty slots. `taskSlot` is kernel enumeration.
Neither form of number conveys authority by itself.

Creation authority is a nontransferable image mask in the caller's kernel TCB
(`createImages`). Bootstrap grants the supervisor bit 0 for catalog image 1.
Images 2–16 are build-issued data rows (`ImageRow`: start, end) that boot loads
with `taskCatalogLoad`; the creation mask comes from the rows loaded.
A child inherits no creation authority. Image 1 is the bounded embedded
`runtimeApprovedStart`/`runtimeApprovedEnd` image: its argument is an exit code;
`0xDEAD` requests an intentional unaligned-read fault. Syscalls never accept
kernel source addresses, arbitrary executable buffers or service names.

Each successful creation installs a separate nontransferable `TaskControl`
capability owned by the creator's *reference*, for that child's *reference*.
The kernel checks the owner and operation rights on every lookup. A guessed
reference, creation capability, diagnostic slot or capability belonging to a
previous supervisor generation cannot control another task. Bootstrap can
also grant specified control rights explicitly before its independent seal.
The supervisor gets inspect/terminate rights over itself to exercise Running
termination; this does not grant foreign control.

| Syscall | Arguments | Required capability | Successful result |
| --- | --- | --- | --- |
| 36 create | image ID | caller's catalog image bit | positive child reference |
| 37 configure | reference, endpoint handle or 0, rights, argument | CONFIGURE (1) | 0 |
| 38 publish | reference | PUBLISH (2) | 0 |
| 39 inspect | reference, writable `TaskEvent*` | INSPECT (4) | 0 and snapshot |
| 40 terminate | reference, final exit code | TERMINATE (8) | 0; self termination does not resume |
| 41 collect | reference, writable `TaskEvent*` | COLLECT (16) | 0 and final event; capability consumed |
| 59 cancel wait | reference | CANCEL (32) | 0 or `-EAGAIN`; current IPC/sleep wait only |
| 76 load | user address, byte length of an ELF image | caller's `IMAGE_LOAD_AUTHORITY` bit | positive child reference; [Files-backed loading](FILES_LOADER.md) |
| 75 lifetime | reference or 0, writable `LifetimeReport*` | caller's catalog image bit; reference also INSPECT (4) | 0 and report; [lifetime contract](LIMITS_AND_LATENCY.md#remaining-lifetime-report-g4) |

Creation grants all six child-control rights (63), including the independent
`TASK_RIGHT_CANCEL=32` from [A4 liveness](IPC_LIVENESS.md); it grants no rights over
unrelated tasks. Endpoint configuration attenuates an existing caller handle
through `handleCopy`. Service receive rights stay with the immutable receiver, while management
rights stay with the creator; [runtime endpoint factories](RUNTIME_OBJECTS.md)
can bind a fresh Service endpoint to an owned Created child. No IRQ, MMIO, DMA, UART or task-creation rights
are inferred from startup arguments. A5 additionally provides a sealed catalog
of trusted ELF images and [atomic private service publication/resolution](SERVICE_RECOVERY.md). Bootstrap device/resource root grants stay sealed; syscall 54 separately
checks a bounded supervisor factory before issuing UART/input broker rights.
User wrappers are in `user/syscalls.m`; ABI types are imported directly from
`src/task/runtime_start.m`.

## Construction, publication and rollback

Shared `taskConstructImage`, `taskInstallRuntimeStart`, `taskPublishChecked`
and `taskDiscardChecked` mechanisms are independent of the sealed bootstrap
wrappers. Boot still chooses its images and resource policy. Runtime chooses
only an approved catalog entry after checking the caller's creation mask.

Reservation places a slot in Created before allocation. Created has no ready
queue entry and cannot run. Runtime publication requires a configured private
RO/NX startup page, owned executable code without W, writable NX stack,
initial user context, valid PTBR, aligned SP and any inherited endpoint still
live. The startup page contains ten words: magic, version, size, reference,
diagnostic slot, data VA, data bytes, endpoint handle, endpoint rights and
argument. The initial r1/r2 name that page and its 40-byte size.

The resource ledger is explicit in the following kernel records. All mutation
runs on one CPU with IRQs excluded. A construction failure has no scheduling
window, and no user-supplied pointer becomes a ledger entry.

| Acquisition | Ledger entry | Rollback / final release |
| --- | --- | --- |
| child capability and completion reservation | `taskControls[]`: owner, reference, rights | clear row on create/configure failure or collection; orphan rows released on supervisor death |
| slot / generation | `tasks[]`: id, slot, Created state | return Empty after complete rollback; preserve generation |
| address-space root and tables | `Task.directory`; allocator owner/purpose/references; MMU initialized bit | `mmuDestroyAddressSpace`, only while root inactive; restores W^X aliases |
| code, data, user stack frames | `Task.pages[]`; allocator and mapping pins | root teardown releases mapped frames; `taskRollback` releases remaining owned frames |
| guarded kernel stack | `kernelStackBottom`/`kernelStackTop`; allocator stack run | `mmuFreeKernelStack`, after moving to another stack |
| attenuated endpoint handle | child's `HandleTable` with persistent generations | `handlesReleaseTask` drops copies and cancels owned endpoints/waits |
| startup frame and mapping | `Task.bootPage`; allocator record and MMU pin | root teardown or `taskRollback` for an unmapped frame |
| IRQ/device grants (boot or bounded runtime UART/input policy) | IRQ grants, MMU resource grants, broker owners, all keyed by reference | masked/revoked at death; MMU grants removed at root teardown; DMA pin held until physical quiescence |

Invalid configuration arguments leave Created intact for retry. Allocation or
startup mapping failure after rights acquisition discards the entire private
construction, releasing the copied handle as well. Publication failure leaves
Created intact for inspect/retry/cancellation. Terminating Created cancels
construction and consumes its control capability; it produces no execution
completion event. Handle generations and reply-call generations are never
reset by construction or collection.

## Completion, cancellation and reclamation

There are as many concurrent slots as this boot's table has (from the installed
RAM, see [init](INIT.md#task-slots)), one reserved control/completion row for
each, and a separate 32-entry diagnostic history ring. Reaped runtime slots become
Empty independently of completion collection. The event remains in its own
reserved row even if the slot is reused. Slow collection cannot overflow or
drop these events: creation returns `-ENFILE` when all rows are occupied.
Ordinary creators have a four-child control quota including uncollected events.
The final two global task slots and control rows are reserved for bootstrap's
sealed recovery policy; its `factoryRecovery` flag is nontransferable. These
limits are enforced before construction; collecting an event refunds its
creator's quota. See [domain accounting](CAPABILITY_TRANSFER.md).
The history ring intentionally overwrites old diagnostics and supplies no
authority. Existing fixed boot/acceptance tasks retain their Dead TCB snapshots;
only runtime children are marked reusable by the runtime policy.

`TaskEvent` contains eleven words: reference, diagnostic slot, state, signed
exit code, flags, CAUSE, EPC, BADADDR, STATUS, PTBR and FCSR. Flags are FAULT=1,
TERMINATED=2, RECLAIMED=4 and QUARANTINED=8. Completion is recorded once at
logical death, with the final exit code and available saved trap diagnostics.
Inspect returns either a live snapshot or the retained completion. Collect is
nonblocking (`-EAGAIN` before death); yield and retry is sufficient for the
minimal supervisor. `-EFAULT` leaves the event and capability intact. Collection
before physical reclamation returns the current flags and never frees DMA or
makes a slot reusable. A supervisor needing reclamation confirmation should
inspect until RECLAIMED, then collect.

Ready termination removes the task from any ready FIFO position. Blocked
termination detaches raw send/receive, AwaitAccept/AwaitReply/accept and IRQ
waits. Endpoint manager/receiver death returns `-EPIPE` once to surviving IPC
waiters; terminated tasks are detached before endpoint revocation so they are
not spuriously awakened. IRQ waits and deadlines are masked and cleared.
On one CPU the only Running target is the caller: its saved frame is finalized
and another task or idle is selected before teardown.

The assembly restore path moves SP to the selected kernel stack before calling
the reaper. The reaper checks that stack and selected PTBR, rejects current or
queued victims, and the MMU teardown rejects an active victim root. It commits
one dead root per call (G5); a dead task keeps everything until its own stage,
and `RECLAIMED` appears in its completion only then. The broker
must report physical quiescence before stack, mappings or task slot can be
released. A BUSY disk DMA keeps the task Dead and quarantined, even if its
completion has already been collected. Late IRQs cannot revive the old task.
A dead supervisor's unpublished children are discarded; published children
continue independently, and their ownerless completion rows are released at
death. This lifetime rule does not impose service restart policy.

## Reference audit before reuse

| Reference holder | Representation and invalidation |
| --- | --- |
| ready FIFO | full `Task.id`; exact `taskGet` on selection; removed at termination |
| endpoint manager | full reference; manager death revokes object regardless of remaining handles |
| endpoint sender/receiver FIFOs | full reference; pinned waits detach at death |
| reply owner | full service reference, checked against current caller |
| reply token's low byte | diagnostic client slot only; persistent per-slot call generation, exact reply owner and endpoint generation provide validation; call generations retire at exhaustion |
| task-local endpoint handles | handle generations survive slot reuse; old handles cannot name newly installed copies |
| IRQ grant | full owner reference plus independent grant generation; masked and cleared at death |
| screen/input/disk broker owners | full references; release or quiescence checks compare the whole word |
| DMA operation owner | full reference; logical cancellation forbids new submissions, physical buffer pin survives BUSY |
| physical allocator / tables / MMU grants | full reference in owner metadata; no truncation; kernel/idle/DMA owners occupy disjoint reserved values |
| ASID | diagnostic slot only; full TLB invalidation on every address-space activation prevents stale translations |
| task controls and completion rows | full owner and target references; completion outlives live slot; collection consumes authority |

The two exceptions requiring code changes were reply-token slot lookup and
endpoint cancellation's task-table enumeration. Both now use `taskSlot`;
all authority and ownership checks still compare generation-bearing values.
See [runtime task acceptance](../tests/RUNTIME_TASKS_ACCEPTANCE.md) for evidence
and the distinction between checked-source and CPU coverage.

## Scoped runtime memory

[Runtime memory](RUNTIME_MEMORY.md) enrolls each new task in a 96-frame budget
before its directory is allocated. The budget includes its private tables,
all user frames and guarded kernel stack; application allocations leave the
16-frame trusted progress reserve available. A creator with CONFIGURE authority
can request a bounded memory capability for the Created child's private eager
regions. Successful publication revokes that foreign loader capability.
Region ownership remains with the child. Death revokes memory authority;
selected-stack, DMA-quiescent reaping also frees private never-mapped/unmapped
regions. Explicitly granted regions with surviving borrowers move to bounded
orphan accounting before the original task's zero-charge budget closes. Their
frames retain the original allocation generation until the last borrower/pin
releases them; the old slot may be reused without owning or refunding those frames. Existing startup and task-control ABI
layouts are unchanged.

With sharing, TaskEvent RECLAIMED confirms retirement of the task's TCB-owned
address space, kernel stack and budget. It can coexist with detached shared
regions retained by borrowers; it does not promise that those independently
pinned frames have returned to the allocator. Such retention is recorded by
the region/grant ledgers and `memoryOrphanPages`, not the DMA QUARANTINED flag.

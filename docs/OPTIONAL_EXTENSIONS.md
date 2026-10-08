# Optional extensions: selection record

A10 decision record, 2026-10-07. This is a selection and entry-contract
document. It adds no kernel mechanism, test or CPU evidence; every extension
below is **deferred** and remains optional. Completion of the single-CPU
lifecycle baseline ([readiness matrix](READINESS_MATRIX.md)) does not depend
on any of them.

## Intended workload

The supported workload is the one the maintained profiles already exercise:

- one CPU (the machine is single-core by design; `HARTID` always reads 0),
  eight task slots and one execution context per task;
- user services for UART/Screen/Input/Disk/Files, a supervisor that creates,
  collects and restarts approved children, and independent compiled
  applications calling those services through 0–32-byte IPC;
- read-only 512-byte sector DMA through the trusted bounce-buffer broker;
- no POSIX layer, networking, shell or persistent writable filesystem. (Superseded for the last three by [G7](GAP_07_APPLICATION_LAYER.md): separate boot profiles now provide a writable filesystem, a shell and a network path; there is still no POSIX layer.)

The numeric profile (RAM range, image sizes, task counts, assumptions) is in
[TARGET_WORKLOAD.md](TARGET_WORKLOAD.md).

None of the measured or accepted scenarios in
[A8](LIMITS_AND_LATENCY.md) or [A9](../tests/ACCEPTANCE_CI.md) is limited by
the mechanisms below, so no extension has a workload requirement today.

## Selection

| Extension | Decision | Why | Entry condition for selecting it later |
| --- | --- | --- | --- |
| Shared-memory regions / bulk grants for I/O | Deferred; cost measured (2026-10-08), not selected | Whole-region grants with a borrower ledger already exist for explicit sharing ([runtime memory](RUNTIME_MEMORY.md)); services move at most 512 bytes per DMA operation and Screen writes through device-granted mappings. The measured cost of chunking is 64 KiB in 4,096 16-byte reads, 7.10 s at 128 MHz ([TARGET_WORKLOAD](TARGET_WORKLOAD.md#bulk-transfer-cost)); the selected profile reads only 16-byte control data, so the entry condition is not met by it. Selecting it would also need the borrower-naming decision in [bulk grants](#shared-bulk-grants), because `grantRegion` names its borrower by full task reference | Select it when a workload reads tens of KiB through Files or Fs; first decide how the grant's borrower is named for the fixed Files service |
| Multiple threads per address space | Deferred | `Task` combines address space, scheduling, handles and one IPC wait. Eight slots and per-task services already give concurrency by process | A service that needs concurrent workers in one address space |
| User fault notification / pager / debugger | Deferred | User faults terminate the task and the supervisor reaps it; supervisor faults panic. Memory is eager, so no workload needs demand paging | Recoverable faults or demand allocation are required by an application |
| General clock, wait sets, notification objects | Deferred | Bounded `sleep`, timed calls, try variants and timed IRQ waits ([IPC liveness](IPC_LIVENESS.md)) cover the supervisor and watchdog | An application that must multiplex several event sources in one task |
| Priorities, donation, CPU budgets | Deferred | FIFO round-robin is the documented baseline; the A8 admission budgets bound shared-server use. No differentiated latency target exists | A stated latency or real-time requirement that round-robin cannot meet |
| Kernel fault recovery / kernel preemption | Deferred | Trap entry keeps its static, non-nesting scratch assumption | Any selected extension that needs a recoverable kernel fault or preemptible kernel |
| SMP | Deferred, out of scope | The CPU is single-core by design. Per-CPU state, locks, memory ordering and remote TLB shootdown would be a separate architecture milestone, not an A10 increment | A different CPU model; it would need its own contract |

POSIX, networking, a shell and a full filesystem stay in the OS-service
roadmap. They are user-space services built on the lifecycle, authority and
recovery contracts, not additions to the kernel mandate.

## Contracts a selected extension must define first

Each extension reuses existing authority, quota and lifetime rules rather than
introducing a parallel model. Before implementation, its design record must
cover the points below; the scenarios give the acceptance shape.

### Shared bulk grants

- **Naming the borrower (checked 2026-10-08):** `grantRegion` names its
  borrower by full task reference, so the lender must know the service's
  reference. A client of a resolver-published service has it: `resolveService`
  returns it as `instance`. A boot-created service (the fixed Input/Disk/Files
  graph) publishes no reference, and `AcceptResult` carries only the length and
  reply token, so a service cannot learn its caller's reference either. A
  client-owned buffer therefore works for resolver-published services on the
  current kernel; reaching the fixed Files service needs either publication
  through the resolver or the authenticated sender reference in the accept
  result (same persistent-generation rules as `collectTransfer`'s sender word).

- **Contract:** ownership stays with the granting region; borrowers hold a
  ledger reference and a PTE ceiling (as in the existing grant). Define a
  maximum transfer size, mapping authority and the synchronization point
  (the IPC message that names the grant).
- **Revocation:** revocation and reclamation are independent of IPC
  completion and of device DMA lifetime. A frame is not reused while any
  borrower alias or DMA pin remains; DMA keeps the existing quiescence rule.
- **User scenario:** a client hands a file service a region, the service fills
  it and replies, the client reads the data without a per-chunk copy.
- **Failure scenario:** revoke while a request is in flight and while DMA is
  busy, in both death orders; no premature physical reuse.

### Threads

- **Contract:** split process lifetime (address space, handle table, budgets)
  from thread lifetime (context, stack, wait, scheduling). Exit or fault of
  one thread must not free state another thread uses.
- **Delegation:** define which threads may `accept`, and whether a reply right
  stays owner-bound to the accepting thread or may be delegated. Default is
  the existing owner-bound one-use right.
- **User scenario:** a service runs several workers on one endpoint.
- **Failure scenario:** one worker exits or faults mid-call while others run;
  its caller gets `EPIPE` once, address space and handles survive, and the
  reply right of the dead worker is not usable by a sibling.

### Recoverable faults and paging

- **Contract:** the fault message is trusted (kernel-built, identifies task,
  address and cause). Resume or context change requires explicit authority
  held by the pager/debugger, with a timeout and failure policy that falls
  back to the current terminate-the-task behavior.
- **Resources:** reserve pager resources up front. A pager dependency cycle
  (a pager faulting on its own pages, or two pagers waiting on each other)
  is contained by a bounded wait that terminates the faulting task.
- **User scenario:** a faulting task is resumed by an authorized pager.
- **Failure scenario:** an unauthorized resume is denied; a dead or stalled
  pager does not stop unrelated applications.

### Event multiplexing

- **Contract:** build on bounded sleep and timed waits; a wait set or
  notification object is an ordinary scoped capability with a generation and
  owner-death retirement, and is cancellable by the supervisor.
- **Scenarios:** one task waits on IPC plus a timer plus an IRQ; owner death
  and cancellation each release it exactly once.

### Scheduling classes

- **Contract:** state the latency or fairness target first. Donation through
  nested IPC must be restored on completion, timeout, cancellation and
  owner death, so cancellation never leaves a raised priority behind.
- **Scenario:** measured latency of a high-priority client behind a
  dependency chain, compared with the round-robin baseline in A8.

### Kernel fault recovery and preemption

- Replace the static trap scratch assumptions with a verified nesting-safe
  entry/context protocol before enabling either; until then the
  non-nesting assumption in [stage 1](01_BOOT_TRAPS.md) stays normative.

### SMP

- Would require per-CPU entry and scheduler state, a lock hierarchy, memory
  ordering rules and remote TLB invalidation, validated on a multi-CPU
  target. Not planned for this machine.

## Evidence rules

Every extension, when selected, follows
[Keeping evidence current](READINESS_MATRIX.md#keeping-evidence-current):
source regressions for admission, exhaustion, rollback, cancellation/death
races and stale authority, plus identified CPU cases before it is marked
CPU accepted. Recording an extension as deferred here is not evidence that it
works, and no row in the readiness matrix claims otherwise.

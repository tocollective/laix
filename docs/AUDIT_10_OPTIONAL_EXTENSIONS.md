# A10. Extensions after the lifecycle baseline

[Audit overview](../../docs/LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_09_ACCEPTANCE_AND_CI.md)

Date: 2026-10-04. Priority: **P3 (optional)**.

These checklists describe proposed work, not completed implementation.

## Dependencies

Stabilize A1–A9 first. Select these features from actual workload requirements; the checklist does not make every extension mandatory.

## Current state and gap

The following are useful choices, not universal microkernel requirements:

- Shared-memory regions or bounded grants for bulk I/O. Define ownership,
  borrower references, revocation and synchronization before replacing copies.
- Multiple threads per address space. Today `Task` combines address-space,
  scheduling, handles and one IPC wait; separate process and thread lifetime
  before adding workers or delegated reply rights.
- User fault notifications and an optional pager/debugger protocol. Current
  user faults terminate the task; supervisor faults panic. Recoverable kernel
  faults would also need a nesting-safe trap/fixup protocol.
- Clock/sleep, wait sets and general notification objects for event-driven
  applications. Timed IRQ waits already provide a limited device facility.
- Scheduling classes, priority inheritance/donation and CPU budgets if needed
  by the workload; SMP requires a separate locking/per-CPU/TLB-shootdown design.

## Implementation approach

A10 is a selection checklist. Shared memory becomes valuable when copying
32-byte chunks dominates a real workload. Multiple threads become valuable
when services need concurrent workers. A user pager is appropriate when
recoverable faults or demand allocation are needed. These choices should
follow the simpler lifecycle and authority contracts.

The existing one-CPU, one-thread-per-task model can support a useful complete
system after runtime management and recovery are added. POSIX compatibility,
networking, a shell and a persistent filesystem can be implemented as later
user services without redefining microkernel completeness.

## Implementation checklist

- [ ] Choose the intended workload and explicitly mark shared memory, threads, paging, priorities and SMP as selected or deferred.
- [ ] For bulk I/O, define shared-region/grant ownership, mapping authority, synchronization and maximum transfer size.
- [ ] Define revocation and reclamation of bulk buffers independently of IPC completion and device DMA lifetime.
- [ ] If workers/threads are selected, separate process address-space/handle lifetime from thread context, stack, wait and scheduling lifetime.
- [ ] Define service receiver and reply delegation rules before allowing worker threads to accept or finish calls.
- [ ] If recoverable user faults are selected, define a trusted fault message, authorized resume/context change and failure/timeout policy.
- [ ] If paging is selected, reserve pager resources and define how pager failure or a pager dependency cycle is contained.
- [ ] Add general clock/sleep and notification/wait facilities if applications require event multiplexing beyond the first supervisor.
- [ ] If priorities are selected, define inheritance/donation through nested IPC and how cancellation restores scheduling state.
- [ ] If kernel fault recovery/preemption is selected, replace static trap scratch assumptions with a verified nesting-safe protocol.
- [ ] If SMP is selected, design per-CPU entry/scheduler state, locks, memory ordering and remote TLB invalidation as a separate architecture milestone.
- [ ] Keep POSIX, networking, shell and full-filesystem requirements in the OS-service roadmap rather than expanding the kernel mandate.

## Acceptance checklist

- [ ] For each selected extension, record an observable user scenario and a failure/teardown scenario before implementation.
- [ ] For shared grants, race revocation with IPC/device completion without premature physical reuse.
- [ ] For threads, exit/fault one worker while others run and verify address-space/handle lifetime plus reply authority.
- [ ] For fault/pager protocols, deny unauthorized resume and keep unrelated applications progressing after pager failure.
- [ ] For scheduling extensions, measure the stated latency/fairness contract under dependency chains and cancellation.
- [ ] For SMP, validate cross-CPU ownership changes, queue synchronization and stale-TLB prevention on a supported multi-CPU target.
- [ ] Keep deferred items visibly optional; completion of the single-CPU lifecycle baseline must not depend on implementing every extension.

## Completion record

When work is accepted, link each completed requirement to its tests and record
the source and image provenance, remaining limitations and CPU acceptance
results. A checked box must not imply a stronger evidence level than the linked
record establishes.

# A1. Runtime task lifecycle and supervision

[Audit overview](../../docs/LAIX_MICROKERNEL_AUDIT.md) · [Next item](AUDIT_02_RUNTIME_MEMORY.md)

Date: 2026-10-04. Priority: **P1**.

These checklists describe proposed work, not completed implementation.

## Dependencies

A2 and A3 supply scoped memory and object authority. A4 supplies bounded waits and cancellation. Define reusable task identity before A5 introduces restart.

## Current state and gap

**Evidence:** [taskCreateImage, taskPublish and taskDiscardCreated](../src/task/task.m)
reject operation after `schedulerStarted`. `MAX_TASKS=8`; successful tasks
remain Dead after reaping, so the eight slots are a lifetime limit, not just
a concurrent-task limit. `taskGet` and resource owners use bare task IDs.
[taskCreateProgram](../src/task/program.m) already validates a limited
ELF/PT_LOAD layout, but accepts only embedded kernel-rodata images and reaches
the same sealed constructor. There is no user task-control syscall in
[the dispatcher](../src/trap/trap.m).

**Consequence:** the boot configuration is the entire process population.
An application cannot spawn another application, and a supervisor cannot
replace a crashed server. Exit/fault information remains in the TCB and UART;
there is no user-facing completion event, wait/join or controlled termination.
`taskAbortBlocked` is a kernel helper restricted to suspended tasks.

**Needed:** introduce a user-space init/process supervisor with scoped task
creation and control authority. Define construction, configuration,
publication, termination, completion notification and final reclamation as
separate operations. Make task identities generation-bearing before recycling
slots; audit endpoint managers, IRQ owners, page owners, queues and reply
records together. Keep diagnostic history outside reusable live TCBs.

**Acceptance:** create/exit substantially more than eight tasks over one boot;
fault one while others exchange IPC; reclaim resources; reject stale task
references; deny unauthorized control; roll back every failed construction.
Start with embedded images if necessary, then add a checked runtime image
source. Dynamic linking, fork and POSIX exec can wait.

## Implementation approach

Start with one scheduling context per task and approved embedded images. The first
runtime supervisor needs launch, completion and terminate operations; fork,
thread groups and signals can follow later. A task object can carry management
rights while its slot number remains a diagnostic detail.

A complete lifecycle should distinguish logical death, published completion,
physical resource reclamation and reuse eligibility. Logical death cancels
communication promptly. Physical DMA may delay reclamation, and a supervisor
must be able to observe that delay without resurrecting the old identity.

## Implementation checklist

- [ ] Define a generation-bearing task reference and distinguish it from a diagnostic numeric slot ID.
- [ ] Specify which capability permits create, configure, publish, inspect, terminate and collect a task; creation authority must not imply control over unrelated tasks.
- [ ] Separate the reusable live task record from retained exit/fault diagnostic history.
- [ ] Audit task references in ready queues, endpoint managers, reply owners, IRQ grants, device owners and allocator metadata before enabling slot reuse.
- [ ] Introduce a runtime Created state that stays unschedulable until context, mappings, startup records and initial rights are valid.
- [ ] Refactor the sealed boot constructor into shared checked construction mechanisms and a separate bootstrap policy.
- [ ] Define an explicit resource ledger and rollback for every allocation/grant performed during runtime construction.
- [ ] Add bounded task completion/fault events for the supervisor, including the final exit code and available trap diagnostics.
- [ ] Implement authorized termination for Ready, Running and Blocked tasks, including removal from queues and cancellation of IPC/IRQ waits.
- [ ] Preserve the existing rule that reaping runs on another selected stack and only after the address space is inactive.
- [ ] Keep DMA-dependent resources quarantined until the broker reports physical quiescence; do not make a quarantined slot reusable.
- [ ] Add a minimal user supervisor that launches an approved image and collects completion without kernel policy for specific services.

## Acceptance checklist

- [ ] Run more than eight sequential task lifetimes in one boot and verify stable resource counts after collection.
- [ ] Terminate tasks in Ready, Running, Raw wait, AwaitAccept, AwaitReply and IRQ wait states; each survivor receives the documented result once.
- [ ] Reuse a slot and prove that old task-control, reply, IRQ and endpoint references cannot affect the replacement.
- [ ] Deny foreign task control and forged task references without changing either task.
- [ ] Inject failure after each construction step and verify that no partial task becomes runnable.
- [ ] Kill a DMA owner while BUSY is held and prove that its stack, mappings, buffer and task identity remain unavailable for reuse.

## Completion record

When work is accepted, link each completed requirement to its tests and record
the source and image provenance, remaining limitations and CPU acceptance
results. A checked box must not imply a stronger evidence level than the linked
record establishes.

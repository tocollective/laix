# A3. Runtime object and resource creation

[Audit overview](LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_02_RUNTIME_MEMORY.md) · [Next item](AUDIT_04_IPC_LIVENESS.md)

Date: 2026-10-05. Priority: **P1**.

Implementation and acceptance are complete for the first scoped runtime.
[Runtime objects](RUNTIME_OBJECTS.md) specifies separate checked interfaces,
factory budgets, immutable receiver binding, rollback and revocation.
[The acceptance record](../tests/RUNTIME_OBJECTS_ACCEPTANCE.md) separates
checked-source adversarial evidence from real CPU execution.

## Dependencies

Define jointly with task/memory authority in A1/A2 and receiver-controlled transfers in A6. A5 requires runtime factories for fresh service instances.

## Current state and gap

The supervisor now holds a bounded endpoint factory, approved task-image
creation, child control and UART/input broker grant authority. Existing A1/A2
interfaces supply task/address-space/memory construction and accounting.
Bootstrap root issuers remain sealed. [objects.m](../src/ipc/objects.m) enforces
runtime mode/quota checks and separate creator/receiver identity;
[control.m](../src/task/control.m) checks narrow runtime device delegation.

The first device set is UART TX and exclusive keyboard events with a masked,
generation-bearing IRQ. Disk DMA and screen/font root reassignment remain A5/A7
work. Receiver-controlled ordinary handle delivery is still A6 work; A3 reserves
two supervisory handle slots against unsolicited ordinary-copy pressure.

## Implementation approach

A practical first factory can be held only by the user supervisor. Applications
ask that supervisor for a private endpoint or child task, and the supervisor
receives only the rights needed to build it. This permits runtime management
without immediately designing arbitrary user-to-user authority trees.

Preserve the useful distinction between an object's management identity and
ordinary send rights. Service restart should normally create a new object
bound to a new task; rebinding a destroyed endpoint would undermine existing
stale-handle guarantees.

## Implementation checklist

- [x] List object types and operations required by the first runtime: endpoints, task control, address spaces, memory and bounded IRQ/device authority.
- [x] Decide whether to extend the handle table to typed objects or use separate scoped interfaces; document the common authorization rules.
- [x] Define factory/root rights and quotas, distinct from rights on an already created object.
- [x] Delegate bounded creation authority from boot to the intended user supervisor.
- [x] Replace unconditional post-boot creation sealing with explicit authority checks while keeping bootstrap-only root issuance sealed.
- [x] Specify Service endpoint creation and immutable receiver binding for one service instance.
- [x] Keep rights attenuation explicit; possession of a numeric object/task ID must not grant access.
- [x] Specify destruction/revocation separately from closing one reference and retain generation checks on every lookup.
- [x] Define ownership of endpoints when their creator and designated service are different tasks.
- [x] Account for factory exhaustion, handle installation failure and transactional rollback of an object with no published references.
- [x] Charge object and wait storage to a defined budget; reserve capacity for supervisory recovery.
- [x] Document whether scoped revocation covers the whole object or one delegated reference; do not imply delegation-tree revocation if it is absent.

## Acceptance checklist

- [x] Create, exchange through and destroy new Raw and Service endpoints after normal user scheduling starts.
- [x] Deny factory calls by applications lacking creation authority and deny rights amplification.
- [x] Destroy an endpoint with queued and accepted calls and verify exactly-once cancellation and reference release.
- [x] Exhaust object/handle budgets and verify recoverable errors plus continued supervisor progress.
- [x] Reuse object slots and reject stale handles; boundary generations retire without wrapping.
- [x] Fault during construction and installation and verify that no unowned live object or hidden root right survives.

## Completion record

[Runtime object acceptance](../tests/RUNTIME_OBJECTS_ACCEPTANCE.md) links each
implementation and acceptance item to its source, tests and CPU evidence, with
source/image hashes and remaining limits. Queued/accepted cancellation, failure
injection, foreign receiver ownership, reserves and boundary generations use
checked-source fixtures; the CPU image exercises every new syscall, 20 Raw and
20 Service lifetimes, quota recovery, stale handle rejection, real timer
preemption, IRQ renewal and final resource reclamation. No box claims CPU
coverage for adversarial cases tested only through checked sources.

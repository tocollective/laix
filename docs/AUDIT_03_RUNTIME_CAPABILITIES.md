# A3. Runtime object and resource creation

[Audit overview](LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_02_RUNTIME_MEMORY.md) · [Next item](AUDIT_04_IPC_LIVENESS.md)

Date: 2026-10-04. Priority: **P1**.

These checklists describe proposed work, not completed implementation.

## Dependencies

Define jointly with task/memory authority in A1/A2 and receiver-controlled transfers in A6. A5 requires runtime factories for fresh service instances.

## Current state and gap

**Evidence:** [endpointBootstrapMode](../src/ipc/objects.m) is the sole
endpoint root issuer and is permanently sealed before user entry.
`taskStart` also seals [MMU resource grants](../src/mm/mmu.m) and
[IRQ issuance](../src/drivers/irq.m). Handle tables contain endpoints;
task control, memory and narrow device rights use separate kernel conventions.
Service receive/manage rights are bound to the original manager and cannot
be transferred to another task.

**Consequence:** existing capabilities can be copied, but a new application
cannot create a private channel or acquire fresh bounded resources. Moving
the supervisor to user space is impossible with the current authority set.

**Needed:** add scoped runtime factories for endpoints and the resource types
chosen for A1/A2/A7. A unified typed handle model is one reasonable approach;
separate checked interfaces can also work. The root should delegate bounded
authority, not expose global owner IDs, kernel pointers or arbitrary physical
addresses. Keep revocation, retirement and allocation rollback explicit.
General delegation-tree revocation is an extension; endpoint-wide destruction
already provides a useful basic revocation mechanism.

**Acceptance:** authorized code creates and destroys private channels during
normal scheduling; unprivileged code cannot mint authority or raise rights;
object exhaustion is recoverable; stale handles cannot address a replacement.

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

- [ ] List object types and operations required by the first runtime: endpoints, task control, address spaces, memory and bounded IRQ/device authority.
- [ ] Decide whether to extend the handle table to typed objects or use separate scoped interfaces; document the common authorization rules.
- [ ] Define factory/root rights and quotas, distinct from rights on an already created object.
- [ ] Delegate bounded creation authority from boot to the intended user supervisor.
- [ ] Replace unconditional post-boot creation sealing with explicit authority checks while keeping bootstrap-only root issuance sealed.
- [ ] Specify Service endpoint creation and immutable receiver binding for one service instance.
- [ ] Keep rights attenuation explicit; possession of a numeric object/task ID must not grant access.
- [ ] Specify destruction/revocation separately from closing one reference and retain generation checks on every lookup.
- [ ] Define ownership of endpoints when their creator and designated service are different tasks.
- [ ] Account for factory exhaustion, handle installation failure and transactional rollback of an object with no published references.
- [ ] Charge object and wait storage to a defined budget; reserve capacity for supervisory recovery.
- [ ] Document whether scoped revocation covers the whole object or one delegated reference; do not imply delegation-tree revocation if it is absent.

## Acceptance checklist

- [ ] Create, exchange through and destroy new Raw and Service endpoints after normal user scheduling starts.
- [ ] Deny factory calls by applications lacking creation authority and deny rights amplification.
- [ ] Destroy an endpoint with queued and accepted calls and verify exactly-once cancellation and reference release.
- [ ] Exhaust object/handle budgets and verify recoverable errors plus continued supervisor progress.
- [ ] Reuse object slots and reject stale handles; boundary generations retire without wrapping.
- [ ] Fault during construction and installation and verify that no unowned live object or hidden root right survives.

## Completion record

When work is accepted, link each completed requirement to its tests and record
the source and image provenance, remaining limitations and CPU acceptance
results. A checked box must not imply a stronger evidence level than the linked
record establishes.

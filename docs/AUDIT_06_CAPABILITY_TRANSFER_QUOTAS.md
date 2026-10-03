# A6. Capability transfer can exhaust a foreign table

[Audit overview](../../docs/LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_05_SERVICE_RECOVERY.md) · [Next item](AUDIT_07_DEVICE_BOUNDARY.md)

Date: 2026-10-04. Priority: **P2**.

These checklists describe proposed work, not completed implementation.

## Dependencies

The unsolicited endpoint-copy restriction can be fixed on the current model. Apply the same consent and accounting rules to the new object types in A1–A3.

## Current state and gap

**Evidence:** [ipcCopy](../src/ipc/ipc.m) accepts any live user task ID as
the recipient and calls `handleCopy`; [handleInstall](../src/ipc/objects.m)
uses the first free recipient slot. Send-right copying requires no recipient
consent, selected receive slot or task-control capability. Each table has 16
entries, and each copy consumes a fresh slot even for the same endpoint.

**Reproduced in checked-source execution:** bootstrap a Raw endpoint for task
1, copy send authority to task 2, select task 2, then invoke
`ipcCopy(sendToken, 3, RIGHT_SEND)` sixteen times. All copies succeed without
task 3 receiving anything. Its table is full; another installation returns
EMFILE. The same unrestricted send-copy path exists for Service endpoints.
This is an availability/isolation issue; the reproduction does not show rights
amplification, privilege escalation or CPU exploitation.

**Needed:** transfer capabilities through a receiver-selected slot or
receiver-issued bounded transfer authority, ideally coupled to IPC delivery.
Specify acceptance, attenuation, failure rollback and reference charging.
Reserve supervisor capacity or quotas so untrusted clients cannot consume
another domain's resource budget. Extend quotas to frames, tasks, endpoints
and pinned buffers as runtime creation becomes available.

**Acceptance:** repeated unsolicited copies leave the recipient unchanged;
accepted transfers install exactly the agreed rights; transfer failure leaks
neither references nor slots; one exhausted domain cannot prevent supervisor
cleanup or service recovery.

## Implementation approach

The current issue concerns who can mutate a recipient's table, not whether the
sender possesses the endpoint. A sender may legitimately own send authority
and still have no authority to spend another task's sixteen slots.

A small first implementation can allow copying within the caller's own table
and require an explicit receiver-issued permission for cross-task transfer.
If transferred handles accompany IPC, use a kernel-defined metadata area;
an integer token placed in ordinary message bytes alone does not install or
authorize a capability in the recipient.

## Implementation checklist

- [ ] Choose a receiver-controlled transfer mechanism: an explicit receive slot, a bounded transfer ticket or a scoped target-table capability.
- [ ] Remove unrestricted installation into an arbitrary live task selected only by task ID.
- [ ] Define which rights are transferable for Raw endpoints, Service endpoints and future task/memory/device objects.
- [ ] Couple transferred authority to authenticated IPC delivery or document a separate atomic installation/notification protocol.
- [ ] Specify slot reservation lifetime, expiry, cancellation and stale-ticket rejection.
- [ ] Validate attenuation, source liveness and recipient consent before changing either table or reference count.
- [ ] Make failed multi-capability transfers transactional, or define explicit partial-result semantics before exposing them.
- [ ] Add per-domain budgets for handles/endpoints/tasks/frames and charge pins or queued resources to a defined owner.
- [ ] Reserve supervisory capacity for cancellation, notification and cleanup when an application budget is exhausted.
- [ ] Define release/revocation rules for delegated references and resource accounting when sender or recipient dies.
- [ ] Add a permanent regression for the sixteen-copy foreign-table exhaustion scenario.
- [ ] Document that attenuation alone prevents rights amplification but does not prevent resource interference.

## Acceptance checklist

- [ ] Attempt sixteen unsolicited copies into another live task and verify that its table and budget stay unchanged.
- [ ] Accept a transfer into a selected slot and verify exact attenuated rights and authenticated notification.
- [ ] Reject forged, stale, already-consumed and expired transfer permissions without installation.
- [ ] Fail a transfer due to full tables, dead recipients or revocation and conserve all references/reservations.
- [ ] Exhaust one client budget while unrelated clients and the supervisor continue allocating within their own budgets.
- [ ] Kill sender and receiver at transfer boundaries and prove no hidden authority or leaked quota remains.
- [ ] Reproduce consent/exhaustion behavior on CPU after source-model regressions pass.

## Completion record

When work is accepted, link each completed requirement to its tests and record
the source and image provenance, remaining limitations and CPU acceptance
results. A checked box must not imply a stronger evidence level than the linked
record establishes.

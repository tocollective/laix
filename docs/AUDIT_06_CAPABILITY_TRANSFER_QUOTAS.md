# A6. Capability transfer can exhaust a foreign table

[Audit overview](LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_05_SERVICE_RECOVERY.md) · [Next item](AUDIT_07_DEVICE_BOUNDARY.md)

Date: 2026-10-04. Priority: **P2**.

Implemented and accepted on 2026-10-05. [Contract](CAPABILITY_TRANSFER.md) ·
[Requirement-to-test mapping and evidence levels](../tests/CAPABILITY_TRANSFER_ACCEPTANCE.md) ·
[Source/image provenance](../tests/CAPABILITY_TRANSFER_PROVENANCE.json).
The original gap below records the pre-remediation finding.

## Dependencies

The unsolicited endpoint-copy restriction can be fixed on the current model. Apply the same consent and accounting rules to the new object types in A1–A3.

## Original state and gap

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

- [x] Choose a receiver-controlled transfer mechanism: an explicit receive slot, a bounded transfer ticket or a scoped target-table capability.
- [x] Remove unrestricted installation into an arbitrary live task selected only by task ID.
- [x] Define which rights are transferable for Raw endpoints, Service endpoints and future task/memory/device objects.
- [x] Couple transferred authority to authenticated IPC delivery or document a separate atomic installation/notification protocol.
- [x] Specify slot reservation lifetime, expiry, cancellation and stale-ticket rejection.
- [x] Validate attenuation, source liveness and recipient consent before changing either table or reference count.
- [x] Make failed multi-capability transfers transactional, or define explicit partial-result semantics before exposing them.
- [x] Add per-domain budgets for handles/endpoints/tasks/frames and charge pins or queued resources to a defined owner.
- [x] Reserve supervisory capacity for cancellation, notification and cleanup when an application budget is exhausted.
- [x] Define release/revocation rules for delegated references and resource accounting when sender or recipient dies.
- [x] Add a permanent regression for the sixteen-copy foreign-table exhaustion scenario.
- [x] Document that attenuation alone prevents rights amplification but does not prevent resource interference.

## Acceptance checklist

- [x] Attempt sixteen unsolicited copies into another live task and verify that its table and budget stay unchanged.
- [x] Accept a transfer into a selected slot and verify exact attenuated rights and authenticated notification.
- [x] Reject forged, stale, already-consumed and expired transfer permissions without installation.
- [x] Fail a transfer due to full tables, dead recipients or revocation and conserve all references/reservations.
- [x] Exhaust one client budget while unrelated clients and the supervisor continue allocating within their own budgets.
- [x] Kill sender and receiver at transfer boundaries and prove no hidden authority or leaked quota remains.
- [x] Reproduce consent/exhaustion behavior on CPU after source-model regressions pass.

## Completion record

All twelve implementation and seven acceptance requirements are linked to
specific implementation paths, checked-source regressions and CPU observations
in the [completion record](../tests/CAPABILITY_TRANSFER_ACCEPTANCE.md).
Receiver-selected expiring permissions replace public foreign-table copies;
installation publishes a kernel-authenticated notification atomically. Ordinary
child/control quotas and recovery reserves supplement existing endpoint/frame
budgets. Memory-grant offers spend lender resources until borrower acceptance.

The CPU regression rejects 640 unsolicited copies, completes forty Raw/Service
transfers and a final sender-death transfer, rejects forged/replayed/cancelled/
expired permissions, restores an exhausted client table, and finishes with zero
references, transfer records or uncollected authority. WRM was not built;
new LA/IX images ran on the existing emulator/ROM. The linked evidence record
states remaining scope and which adversarial cases use checked-source evidence.

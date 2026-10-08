# Receiver-controlled capability transfer and domain budgets

Date: 2026-10-05. Normative A6 contract. [Acceptance and provenance](../tests/CAPABILITY_TRANSFER_ACCEPTANCE.md).

Attenuation prevents rights amplification, but does not prevent resource
interference: a send-only holder must not spend another task's handle slots.
`copyHandle` (17) therefore copies only within the calling task's table. A live
foreign task returns EPERM, even when the source has the requested rights.
Trusted boot setup, CONFIGURE over an unpublished child, and receiver-invoked
service resolution use separate kernel-only installation paths.

## Consent and atomic notification

The receiver reserves a specific free handle slot for one exact sender task
reference and one exact rights mask. The sender commits one live endpoint
capability under that permission. Kernel installation and publication of an
authenticated completion record are indivisible under IRQ exclusion. The
receiver collects that record through saved syscall registers. Ordinary IPC
bytes may carry a ticket or an acknowledgement, but never install a capability
or authenticate the sender. This is a separate atomic installation/notification
protocol; capability metadata is not embedded in the ordinary payload ABI.

| Syscall / wrapper | Arguments | Result |
| --- | --- | --- |
| 66 `reserveTransfer` | r1=slot (1..16), r2=sender task reference, r3=exact rights, r4=lifetime seconds (1..60) | r1=positive ticket or -errno |
| 67 `commitTransfer` | r1=source endpoint handle, r2=receiver task reference, r3=ticket, r4=exact rights | r1=0 or -errno |
| 68 `cancelTransfer` | r1=ticket, receiver only | r1=0 or -errno |
| 69 `collectTransfer` | r1=ticket, receiver only | r1=installed handle or -errno; r2=authenticated sender reference, r3=exact installed rights |

All calls advance EPC exactly once. Calls 66–68 preserve r2..r31/FCSR; call
69 preserves r4..r31/FCSR and zeros identity/rights outputs on errors. The user
assembly helper captures all three notification words in `TransferResult`.
There is no post-install user-memory copy in the kernel that could fail after
installation. A pointer passed to the user helper must be writable user memory,
as for the existing `AcceptResult` helper.

Tickets are not bearer authority: the kernel records the exact sender and
receiver lifetimes. Their generation is persistent per receiver TCB slot,
advances on issuance, and retires at 0x7fffff instead of wrapping. Both the
receiver task reference and ticket generation must match. A guessed ticket
from another caller, another task lifetime, or another reservation is rejected.
Consent permits the named sender to select any of its live endpoint handles
that satisfies the exact agreed mask; it does not preselect an endpoint identity.

A receiver can own one reservation or uncollected notification at a time.
Issuance reserves one free, nonretired ordinary handle slot without pinning an
endpoint. Ordinary installs skip it. Full/retired slots return EMFILE; an
outstanding record returns EBUSY. Invalid rights/slot/lifetime return EINVAL;
absent or nonlive peers return ESRCH; retired ticket generations return EOVERFLOW.

The hardware COUNT deadline is checked at commit, collect, cancel, issuance and
each timer IRQ, including when no IPC task is blocked. Expiry and cancellation
release the slot without changing its handle generation or any reference count.
The deadline cannot be extended; issue a fresh ticket after cancelling. Expired,
cancelled, forged and consumed tickets return EBADF. A delivered notification
is bounded receiver-owned storage and does not expire; collecting it releases
only the metadata. The installed handle remains until close or task teardown.
A receiver cannot cancel a completed installation; it collects and closes it.

## Rights and failure rules

| Object / authority | Transfer rule |
| --- | --- |
| Raw endpoint SEND / RECEIVE | Nonempty subset of sender rights; exact receiver-agreed mask |
| Service endpoint SEND | Same rule |
| Service endpoint RECEIVE | Immutable receiver only; creator or receiver must be the source; initial creator-to-Created-child setup uses CONFIGURE |
| Endpoint MANAGE | Creator-local only; never delegated across tasks |
| Endpoint factory / recovery policy | Bootstrap-only, nontransferable |
| Task control / task factory | Nontransferable; scoped kernel owner/image policy |
| Memory space / region | Nontransferable; separate explicit grant offers |
| Memory grant | Lender-owned offer; exact borrower explicitly maps within its own window and the permission ceiling |
| IRQ / device / broker authority | Nontransferable; checked task configuration and exclusive broker policy |
| Future task/memory/device types | Deny transfer by default; enabling it requires typed consent, attenuation, lifetime and ownership accounting equivalent to this contract |

Commit first validates caller, live recipient, ticket/lifetimes, exact consent,
source token, source rights, source liveness, creator/receiver restrictions and
reserved slot capacity. Only then does it advance the destination handle
generation and charge one reference and any usable receive reference. Rejected
source/rights/revocation/capacity attempts leave a still-valid reservation
available for retry or receiver cancellation, with no new slot/reference charge.
Expiry or peer death instead releases it. Commit succeeds once and publishes
notification exactly once. No array/batch/multi-capability operation is exposed;
one ticket commits one handle atomically. Multiple separate commits are separate
transactions and carry no all-or-nothing group promise.

Creator/Service receiver death revokes endpoints under the existing lifetime
policy. A sender's death before commit cancels its pending permissions. A
sender's death after commit leaves the installed independent reference and
notification with the receiver; creator death can make that reference stale.
Notification authenticates installation, not continued endpoint liveness: normal
handle lookup still checks object state/generation. Recipient death clears its
reservation/notification and closes every installed reference. A noncreator
sender's death does not revoke an already accepted live endpoint. There is no
delegation-tree revocation. Stale references remain charged until closed.

## Resource ownership and progress

A domain in this runtime is one generation-bearing task lifetime, not a group
of arbitrary cooperating tasks. Fixed storage bounds do not constitute an
unbounded multi-domain allocator.

| Resource | Charge / cap | Release and protected capacity |
| --- | --- | --- |
| Handles | Recipient table, 16 entries including any reservation | Cancel/expiry/death release reservations; close/death release handles; recovery-policy owners reserve slots 15/16 for factory roots |
| Transfer records | One fixed record per task slot | Collect/death clear notification; no notification allocation on commit |
| Endpoints | Creator, sealed quota at most 12; destroyed-but-pinned objects still count | Final reference releases charge; ordinary factories cannot use final two global object slots |
| Tasks and completion rows | Creating domain, four ordinary child controls including uncollected events | Collect/discard refunds; supervisor death removes private children/events; published children run independently and release orphan controls on completion |
| Task global slots / control rows | One slot and one control row per task slot, sized from RAM at boot ([init](INIT.md#task-slots)) | Final two slots and control rows exclude ordinary runtime creation; bootstrap recovery policy may use them |
| Frames and private tables/stacks | Allocation task, 96 physical frames including task construction | Existing teardown/refund rules; 16 physical frames reserved for trusted kernel/idle/broker operations |
| Memory spaces / regions | Four caller space rows; eight allocation-owner regions, each at most sixteen frames | Close, release and existing orphan reclamation; global fixed ledgers remain bounded |
| Grant offers / lease pins | Lender, eight grant rows and one lease pin per region frame per offer | Revoke/close/death drops pins; offers consume no borrower grant quota |
| Accepted grant mappings | Borrower, eight simultaneously mapped grant rows plus borrower page-table frames | Explicit final unmap or teardown returns mapping charge; lender frames retain their original owner |
| Queued IPC, snapshots and reply pins | One fixed wait/message/reply record per task; fixed queues in endpoint storage | Serialized delivery/cancel/timeout/death drops exactly one wait reference |
| DMA / device pins | Existing bounded exclusive broker owner and generation | Quiescence required before release; quarantined pins remain charged and cannot be reused |

The recovery permission is the sealed, nontransferable `factoryRecovery` policy
already granted to trusted supervisors by bootstrap. It now also selects task
and control-row reserve access. Cancellation, revocation, completion collection,
notification collection and cleanup allocate no new handles, tasks or frames.
Exhausting an application budget does not authorize spending a peer budget or
a protected reserve. Trusted bootstrap must leave reserves available, and
trusted supervisors must budget their own retained references/events. Generation
retirement permanently reduces capacity. Global finite pools and orphan ledgers
can still return capacity errors; this contract does not promise progress after
arbitrary aggregate overcommit or hardware DMA that never becomes quiescent.

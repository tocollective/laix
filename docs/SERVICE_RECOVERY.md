# Private service supervision and discovery

The `recovery` profile grants a user supervisor a sealed, immutable image
catalog and bounded factories. User code in
[`user/recovery/policy.m`](../user/recovery/policy.m) and
[`supervisor.m`](../user/recovery/supervisor.m) controls launch order, failure
handling and retry limits. Kernel code supplies construction, atomic
publication, private resolution and physically safe Disk handover.

```sh
LAIX_CONSOLE=recovery sh laix/build.sh
LAIX_CONSOLE=recovery sh laix/run.sh --headless --no-net
```

This profile runs a supervisor, stateless Echo, read-only Disk, Files and a
consenting client. Existing UART/screen/simple profiles retain their startup
ABIs and policies. The recovery image catalog consists of Echo (2), Disk (3),
Files (4) and the supervisor/client image (5); legacy approved child image 1
remains available under its separate mask. Images are validated embedded ELF
segments, never user addresses or arbitrary executable bytes. Catalog
registration and private supervisor-root issuance close with task-control
sealing. Children inherit neither the catalog mask nor factory rights.

## Identities and wire generations

| Identity | Meaning and lifetime |
| --- | --- |
| Service name | Supervisor-local integer 1–4, used for policy; no global namespace or authority |
| Service instance | Full generation-bearing task reference; changes on replacement, including failed reservations |
| Protocol version | Existing request/response headers and operation layout; stays at version 1 for Disk/Files |
| Resource generation | Positive supervisor-issued incarnation, shared by a Disk/Files pair; incremented for replacement, never reused under that supervisor/name |
| Endpoint / handle / IRQ / reply token | Separate existing generation-bearing authority; none can be rebound to another instance |

`RecoveryStart` is 56 bytes. Its first ten words are the unchanged
`RuntimeStart` layout, with `bytes=56`; the additional words are dependency
send handle, IRQ token, resource generation and supervisor reference. The
startup page is private RO/NX. Its instance identity is `reference`, distinct
from `generation`. Generic runtime children still receive the 40-byte record;
legacy simple services retain their 64-byte `ServiceStart`.

Disk and Files keep their wire layouts. Request/response word 1/2 respectively
names the resource generation (the header remains the protocol version).
Recovery instances initialize it from trusted startup. A stale generation on
a fresh endpoint produces protocol `-EPIPE`, without touching Disk or killing
the service. Response validation checks the newly resolved generation.
Legacy simple-profile generation 1 describes its single immutable boot
incarnation only. Resource generations are bounded at `0x7fffffff`; publishing
a non-increasing generation fails instead of wrapping. Supervisor replacement
has a new owner reference and no access to its predecessor's namespace.

## Authorized runtime ABI

All operations run on one CPU with IRQs excluded. Task references and names
select records; kernel-owned capabilities authorize operations.

| Syscall | Arguments | Contract |
| --- | --- | --- |
| 61 `publishService` | name, task reference, root handle, generation | Requires private supervisor root and owned child control; validates configured service, endpoint receiver, generation, dependencies and registry capacity; atomically publishes a Created task and its resolver entry |
| 62 `resolveService` | name, writable `ServiceResolution*` | Requires caller enrollment in that supervisor/name; installs SEND only in the calling task, with explicit consent |
| 63 `allowServices` | owned Created child, name mask | CONFIGURE plus supervisor root; enroll before startup configuration/publication |
| 64 `withdrawService` | name, negative status | Supervisor-only; `-EAGAIN` means recovering, `-EPIPE` means unavailable/quarantined |
| 65 `configureService` | child, receive root, dependency root or 0, generation, IRQ | Supervisor plus CONFIGURE; installs attenuated handles and private startup transactionally |
| 54 `grantTaskDevices` | owned Created child, device mask | Existing factory now additionally permits exclusive read-only Disk under an explicitly granted DISK bit |

There are eight private registry rows, four names per supervisor, eight sealed
supervisor roots, eight concurrent task slots and the existing endpoint,
handle, completion and memory quotas. A row holds an owner-local handle token,
not an extra endpoint pin. Closing/revoking that handle makes the row
unresolvable. Supervisor death clears its rows and root authority. Resolution
checks the full owner and instance references, live endpoint and name mask;
guessing names, task references or tokens grants nothing.

`ServiceResolution` contains four words: newly installed handle, instance
reference, resource generation and name. Invalid output memory rolls back the
new handle. Full client tables fail with `-EMFILE`, without changing an existing
handle. A missing/dead current entry returns `-EAGAIN`; a withdrawn quarantined
entry returns `-EPIPE`. The kernel does not install handles into another task
on the supervisor's behalf. This resolver path does not claim to implement the
separate A6 general capability-transfer contract.

The user launch path finishes image validation, allocation/mapping, resource
issuance and startup copies before `publishService`. That syscall checks
registry capacity before scheduling the child, then commits the task and
registry without a scheduling window. Invalid contexts, stale dependencies,
a full registry or rejected generation advertise no partial replacement.
The syscall also accepts an already published controlled service, for callers
that deliberately separate task launch from discovery; the supplied policy
uses atomic publication. Pre-publication failures discard construction;
post-publication failures terminate/collect the owned child and close roots.

## Failure events, dependencies and reconnecting clients

The watchdog polls scoped `TaskEvent` snapshots once per second for faults,
normal exits or termination. Calls have a five-second deadline. A client can
report a failed/stalled current incarnation on its private Service control
channel; the supervisor acknowledges the report before recovery. Reports carry
the resource generation, so a delayed report cannot retire a later instance.
IPC cancellation/termination completes blocked requests through A4's single
completion path; old handles remain `-EPIPE`, then become `-EBADF` after close.

Recovery hides downstream Files, retires it, then retires Disk. It waits for
physical reclamation and collects completion capabilities. It creates Disk
with fresh task/endpoint/IRQ/startup identities first, then creates Files with
an attenuated send capability to that Disk and the same new resource generation.
Dependencies are checked both at configuration and publication. Echo recovers
independently. The same `recoverService` mechanism supports other stateless
services; additional dependency graphs must declare their ordering in user
policy, rather than rely on names to imply dependencies.

A client observes `-EPIPE`, timeout or invalid protocol response, reports the
failed incarnation, closes its old handle and explicitly resolves again.
Polling with one-second sleeps discovers a replacement after the blocked call
has completed. There is no automatic rebinding, unsolicited installation or
blocking resolver wait. Recovery tests deliberately retain old handles until
new ones are resolved to verify that they cannot reach replacements.

## State, retries and bounds

Echo restores no state. Disk reconstructs the checked immutable boot extent;
Files reconstructs its dependency and validates extent/liveness for every
request, including stat/EOF. Pending replies and partial response buffers are
lost/cancelled. No partial read payload becomes a successful response. Keyboard
queue retention and writable filesystem transactions are outside this profile.

Read/stat/echo operations are idempotent and may be retried after reconnect.
Timeout or death does not roll back a server-side effect. No write or other
non-idempotent operation is supported by these protocols; such an extension
must provide stable request IDs, duplicate suppression and an explicit result
for an operation whose commit is unknown, before enabling automatic replay.

Each managed service gets at most five construction attempts per boot (initial
launch plus four replacements), including failed attempts. Backoff is 1, 2,
4, 8 seconds; it sleeps rather than spinning. Completion/reclamation polling
has a five-second budget. Exhaustion or failed recovery marks only the affected
service chain unavailable. Unrelated tasks remain runnable. Permanent
quarantine retains its completion capability and physical resources; it is not
reported as successful recovery or allocator leakage.

## Physical device handover

Disk factory preflight checks the old owner is dead/retired and observes DMA
quiescence before issuing a new IRQ generation. It never acknowledges medium
CHANGED to make replacement appear valid. Medium removal/change permanently
invalidates this boot resource. Screen/font device reassignment remains under
its separate boot policy; this profile grants no arbitrary MMIO/DMA authority.

Owner death masks/revokes communication and IRQ authority immediately. The
bounce-buffer allocator pin survives BUSY, including late completions. BUSY
must clear before the pin is released or Disk is regranted. Failed preflight
issues no IRQ and changes no owner. A watchdog observing unreclaimed resources
for five seconds withdraws the resource as unavailable/quarantined and stops
automatic retries. WRM has no per-device DMA abort: indefinite BUSY cannot be
recovered successfully without reset or future hardware support. No path frees
the pin to fake progress.

See [acceptance and provenance](../tests/SERVICE_RECOVERY_ACCEPTANCE.md) for the
requirement-by-requirement evidence and explicit source/CPU boundaries.

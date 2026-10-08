# Scoped runtime objects and factories

The first runtime uses separate checked interfaces. Endpoint handles remain
endpoint-only; task controls, memory spaces/regions/grants and IRQ/device
records have their own typed kernel ledgers. An integer from one interface
never supplies authority in another. Every public operation resolves the
calling task, the record's owner, required rights, lifetime and generation
under single-CPU IRQ exclusion. Kernel pointers, arbitrary owner IDs, physical
addresses and MMIO ranges are never accepted as creation authority.

## Object inventory and authority

| Resource | Creation/root authority | Object operations and bounds |
| --- | --- | --- |
| Raw/Service endpoint | nontransferable mode mask, object quota and recovery flag in the caller's `HandleTable` | create, attenuate/copy, close, destroy; 16 objects, 16 handles/task, quota at most 12 objects/creator |
| task control | nontransferable approved-image mask in `Task.createImages` | configure, publish, inspect, terminate, collect; one slot and one control/completion row per task slot (from RAM at boot), four ordinary children/creator; [task contract](RUNTIME_TASKS.md) |
| address space and eager region | self authority or CONFIGURE control over an unpublished child; budget enrolled before root allocation | allocate/release, map/unmap/protect, populate, close; 96 frames/task and a 16-frame trusted reserve; [memory contract](RUNTIME_MEMORY.md) |
| sharing grant | explicit owner-held SHARE authority and named borrower | map/close, bounded grant and borrower ledgers; both death orders retain pins and accounting |
| keyboard IRQ and input broker | nontransferable `Task.deviceFactory` INPUT bit plus CONFIGURE over an unpublished child | one exclusive keyboard subscriber; generation-bearing IRQ wait/complete and checked event reads |
| UART TX broker | nontransferable `Task.deviceFactory` UART_TX bit plus CONFIGURE over an unpublished child | checked byte output, no MMIO or arbitrary register access |

`supervisorBootstrap` grants only the intended supervisor endpoint modes 0/1,
quota 12, recovery access, catalog image 1 and UART_TX/INPUT grant authority.
Applications receive endpoint references or child control grants through the
existing checked paths; copying an endpoint never copies any factory right.
All other boot profiles keep their existing resource policy. `taskStart`
continues sealing endpoint root issuance, task-control root grants, MMU device
resource grants and bootstrap IRQ issuance. Runtime creation reaches separate
authorized mechanisms; it does not reopen these roots.

The original A3 profile delegates UART and keyboard only. A5 adds a separate
[recovery profile](SERVICE_RECOVERY.md) with private supervisor roots, an
immutable ELF catalog and an explicit read-only Disk factory bit. Disk handover
requires physical quiescence; screen/font mappings and arbitrary MMIO/DMA
issuance remain under their boot policy. General device interfaces belong to A7.

## Runtime ABI

| Syscall | Saved arguments | Result |
| --- | --- | --- |
| 53 `createEndpoint` | r1: mode (Raw=0, Service=1); r2: receiver reference, zero selects self | positive task-local root handle or negative errno |
| 54 `grantTaskDevices` | r1: child reference; r2: nonempty UART_TX/INPUT mask | keyboard IRQ token if INPUT requested, zero for UART only, or negative errno |

User wrappers are in `user/syscalls.m`. These syscalls preserve the other saved
registers and FCSR and consume EPC exactly once. Factory absence and forbidden
mode/device rights return `-EPERM`; unknown endpoint modes and a foreign Raw
receiver return `-EINVAL`. An unauthorized foreign Service receiver is denied
with `-EPERM`. An owned receiver must be Created and unconfigured (`-EBUSY`
otherwise). Task references select records; CONFIGURE in the caller's control
ledger, rather than knowing a reference, authorizes the selection.

Device grants also require a Created, unconfigured child with no previous
device grant. INPUT first reserves the masked keyboard IRQ, then initializes
the exclusive event broker. Failure releases the reservation and returns
`-EBUSY`, without adding device rights. Timer, screen/font and arbitrary IRQ lines cannot be requested through syscall
54. The A5 supervisor may additionally request exclusive DEVICE_DISK, with
quiescence preflight, a fresh disk IRQ and the immutable boot-medium broker;
the A3 supervisor and ordinary callers have no such factory right. Pass its IRQ result to the
child through `configureTask`'s argument if the user protocol needs it; startup
arguments themselves never authorize device access. Configuration failure,
Created cancellation and task death release the broker and mask/revoke IRQs.
Grant generations advance on issuance and retire at `0x7FFFFF`, never wrap.

## Service identity and ownership

A Service endpoint's `manager` field is its immutable receiver reference,
including task generation. Its separate `creator` is the destruction authority
and budget owner. Self creation sets both to the caller. A foreign receiver
must be the creator's unconfigured Created child. There is no rebinding API:
a replacement service receives a fresh task reference and a fresh endpoint.

The creator's root has SEND/RECEIVE/MANAGE (7). For a foreign Service receiver,
RECEIVE is staging authority for initial attenuation into that child's table;
it cannot authorize the creator to accept or reply. `configureTask(child,
root, SEND|RECEIVE, argument)` installs the child's reference transactionally.
Receive copies can go only to the bound receiver, from the creator or receiver;
management copies stay with the creator. Ordinary send copies can go to live
peers. Every copy remains a subset of its source rights. No ID or factory
possession grants access to an existing object without its handle.

Only usable receiver handles count in `receiveReferences`; staging rights in
the creator's table do not keep a service alive after its last real receiver
closes. Closing that last receiver revokes the Service endpoint. Before initial
configuration, a foreign Service endpoint has no usable receive reference and
is owned by its creator; cancelling the child revokes it, and closing the only
root releases it. Failed handle copying leaves construction available for
retry. Failed startup allocation/mapping discards the child and revokes its
bound endpoint, while the creator can still close its destroyed root.

Creator death revokes its objects, including endpoints received by another
task, even if it closed its last management handle. Receiver death separately
revokes its Service endpoints. Other peer deaths release their own references.
Published children otherwise retain A1's independent lifetime policy.

## Close, destruction and rollback

Close removes one reference. Destroy requires a live management handle in the
creator's table and revokes the whole object. Queued sends/calls, accept waits
and accepted calls are detached using the existing IPC cancellation path,
release their wait pins and complete surviving callers with `-EPIPE` once.
Accepted reply tokens become invalid immediately. Duplicate destroy/reply
cannot release a pin or wake a task again.

Destroyed copies remain closable and pin both storage and their creator's
quota until their final reference closes. They cannot address a replacement:
both handle and captured endpoint generations are checked. Handle generations
survive task-slot reuse and close; exhausted handle slots retire. Endpoint
slots retire at generation `0xFFFFFFFF`. Reclamation clears both ownership
identities but preserves generations.

Construction initializes one unused object under IRQ exclusion, then installs
its root. A failed root installation releases the unpublished zero-reference
object, clears creator/receiver and leaves no handle or hidden factory right.
Its consumed generation is preserved. There are no endpoint allocation steps
that can expose a partial object to user scheduling. Installing child rights
uses A1's existing configuration rollback.

This is whole-object revocation plus individual reference close. There is no
delegation-tree revocation or API to revoke just a peer's delegated reference.

## Storage accounting and recovery

Every creator is charged one object slot for each referenced endpoint, including
Destroyed endpoints and endpoints created by its boot policy. Scanning the
16-object ledger enforces its factory quota before construction; changing the
receiver never transfers that charge. Both eight-entry endpoint wait queues
are part of that fixed slot's storage, charged even when empty. Each task has
one fixed IPC wait/snapshot/reply record and at most one outstanding wait,
charged to its TCB/task slot; wait pins cannot allocate additional unbounded
storage. Installed handles consume the recipient's fixed handle table.

The last two endpoint slots are inaccessible to ordinary runtime factories.
Recovery factories may use them. Ordinary handle copies into a recovery
factory owner's table leave its last two handle slots available for root
installation. Closing, destroying, cancelling and collecting require no new
endpoint/handle allocation, and A1 reserves completion storage before launching
a child. These reserves protect supervisory progress against ordinary endpoint
and handle pressure; they do not defeat generation retirement or overcommitted
trusted boot policy. Boot must leave those resources available if it delegates
a recovery factory.

Quota/pool exhaustion returns `-ENFILE`; handle exhaustion/retirement returns
`-EMFILE`. Failed root installation restores the object charge. Retaining stale
copies retains charges intentionally. [Receiver-controlled capability transfer](CAPABILITY_TRANSFER.md)
now rejects unsolicited foreign copies and adds selected-slot consent, atomic
notification, ordinary task-domain quotas and task/control recovery reserves.

## Acceptance

[Runtime object acceptance](../tests/RUNTIME_OBJECTS_ACCEPTANCE.md) maps all A3
items to checked-source tests and the dedicated CPU image, with artifact/source
provenance and explicit limits. Reproduce without building WRM:

```sh
python3 -m unittest discover -s laix/tests -p 'test_runtime_objects.py'
LAIX_FIXTURE=objects sh laix/build.sh
python3 laix/tests/probe_runtime_objects_cpu.py laix/build/objects.img laix/build/objects.map
```

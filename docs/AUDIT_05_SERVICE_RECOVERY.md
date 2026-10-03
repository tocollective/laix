# A5. Service restart and discovery

[Audit overview](../../docs/LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_04_IPC_LIVENESS.md) · [Next item](AUDIT_06_CAPABILITY_TRANSFER_QUOTAS.md)

Date: 2026-10-04. Priority: **P1**.

These checklists describe proposed work, not completed implementation.

## Dependencies

Requires runtime tasks/memory/factories from A1–A3, bounded failure handling from A4 and safe transfer from A6. Device regrant must respect A7 DMA quiescence.

## Current state and gap

**Evidence:** boot policy is embedded in
[bootstrap.m](../src/kernel/bootstrap.m),
[service_bootstrap.m](../src/kernel/service_bootstrap.m) and
[simple_bootstrap.m](../src/kernel/simple_bootstrap.m). Discovery consists
of pre-issued handles in validated startup records. There is no runtime
registry/rebinding path; [the simple-service contract](SIMPLE_SERVICES.md)
explicitly excludes restart. Existing endpoints correctly become unusable
when their manager dies.

**Consequence:** fault containment works, but service availability is lost
for the rest of the boot. Files treats a dead/malformed Disk dependency as
terminal, propagating failure to its own clients. A stale handle should keep
returning EPIPE; silently attaching it to a new instance would break identity.

**Needed:** implement supervised restart after A1–A4. Construct a fresh task,
endpoint and grants; publish them transactionally; distribute fresh send
capabilities through an authorized resolver or supervisor protocol. A public
global namespace is unnecessary. Define dependency ordering, retry limits,
service instance identity and state recovery. Fixed wire generation 1 in the
current immutable file/disk protocols does not define replacement semantics.

**Acceptance:** repeated server failures recover without reboot; old endpoint,
task, reply and IRQ references stay invalid; callers explicitly reconnect;
unrelated services retain progress. For DMA owners, regrant only after physical
quiescence. [The broker](../src/drivers/service_devices.m) correctly
quarantines active DMA: WRM disk DMA has no per-device abort. If BUSY never
clears, safe successful restart of that device is unavailable without machine
reset or a future hardware abort mechanism. Do not free the pin to fake recovery.

## Implementation approach

Begin with restart of a stateless non-DMA service. Once fresh task and endpoint
identities work, add dependency recovery and device handover. The resolver can
be private to a supervisor and its children; a global nameserver is not needed
for this first contract.

Separate service restart from device reset. Restarting user code cannot abort
an outstanding physical DMA operation on current WRM hardware. A dead service
can release communication authority immediately, but hardware and memory
handover may need to wait or remain unavailable until machine reset.

## Implementation checklist

- [ ] Define a user supervisor policy for launch order, required dependencies, failure events and recovery limits.
- [ ] Define service instance identity separately from protocol version, resource generation and human-readable service name.
- [ ] Construct each replacement with a fresh task identity, endpoint, startup record and scoped resource grants.
- [ ] Publish replacements only after all fallible image, mapping and authority work succeeds.
- [ ] Implement an authorized resolver or supervisor exchange that installs fresh send handles in consenting clients.
- [ ] Keep old handles revoked; do not silently rebind them to the replacement instance.
- [ ] Define client reconnect behavior and how blocked clients learn that a new instance is available.
- [ ] Define dependency recovery for Files/Disk and other service chains, including downstream restart ordering.
- [ ] Specify which state is restored, lost or reconstructed; handle duplicate/non-idempotent requests explicitly.
- [ ] Add bounded restart attempts and backoff so repeated crashes cannot exhaust memory or monopolize scheduling.
- [ ] Regrant IRQ/device authority only after old operations are quiescent; report permanent quarantine when a device cannot safely recover.
- [ ] Update fixed wire generation conventions to describe replacement resource instances without accepting stale requests.

## Acceptance checklist

- [ ] Crash and restart a non-DMA server repeatedly during normal scheduling with no growth in live resource usage.
- [ ] Keep old task/endpoint/IRQ/reply references and verify that none can access the replacement.
- [ ] Reconnect consenting clients explicitly and complete subsequent requests through their new handles.
- [ ] Fail Disk during a Files call; cancel the chain, recover dependencies in order and perform a later successful read.
- [ ] Hold device BUSY during owner death and deny replacement submission/regrant until physical quiescence.
- [ ] Hold BUSY indefinitely and report unavailable/quarantined state while unrelated tasks continue.
- [ ] Inject failure at every replacement construction/publication step and keep the resolver from advertising a partial instance.
- [ ] Run a CPU recovery scenario across multiple failure cycles with matching source/image provenance.

## Completion record

When work is accepted, link each completed requirement to its tests and record
the source and image provenance, remaining limitations and CPU acceptance
results. A checked box must not imply a stronger evidence level than the linked
record establishes.

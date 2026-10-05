# A5. Service restart and discovery

[Audit overview](LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_04_IPC_LIVENESS.md) · [Next item](AUDIT_06_CAPABILITY_TRANSFER_QUOTAS.md)

Date: 2026-10-04. Priority: **P1**.

Status: implemented and accepted at the evidence levels in the
[recovery contract](SERVICE_RECOVERY.md) and
[source/CPU acceptance record](../tests/SERVICE_RECOVERY_ACCEPTANCE.md).
The original gap analysis below is retained as the audit baseline.

## Dependencies

Requires runtime tasks/memory/factories from A1–A3, bounded failure handling from A4 and safe transfer from A6. Device regrant must respect A7 DMA quiescence.

## Original state and gap (2026-10-04)

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

- [x] Define a user supervisor policy for launch order, required dependencies, failure events and recovery limits. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#implementation-evidence))
- [x] Define service instance identity separately from protocol version, resource generation and human-readable service name. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#implementation-evidence))
- [x] Construct each replacement with a fresh task identity, endpoint, startup record and scoped resource grants. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#implementation-evidence))
- [x] Publish replacements only after all fallible image, mapping and authority work succeeds. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#implementation-evidence))
- [x] Implement an authorized resolver or supervisor exchange that installs fresh send handles in consenting clients. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#implementation-evidence))
- [x] Keep old handles revoked; do not silently rebind them to the replacement instance. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#implementation-evidence))
- [x] Define client reconnect behavior and how blocked clients learn that a new instance is available. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#implementation-evidence))
- [x] Define dependency recovery for Files/Disk and other service chains, including downstream restart ordering. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#implementation-evidence))
- [x] Specify which state is restored, lost or reconstructed; handle duplicate/non-idempotent requests explicitly. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#implementation-evidence))
- [x] Add bounded restart attempts and backoff so repeated crashes cannot exhaust memory or monopolize scheduling. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#implementation-evidence))
- [x] Regrant IRQ/device authority only after old operations are quiescent; report permanent quarantine when a device cannot safely recover. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#implementation-evidence))
- [x] Update fixed wire generation conventions to describe replacement resource instances without accepting stale requests. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#implementation-evidence))

## Acceptance checklist

- [x] Crash and restart a non-DMA server repeatedly during normal scheduling with no growth in live resource usage. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#acceptance-evidence))
- [x] Keep old task/endpoint/IRQ/reply references and verify that none can access the replacement. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#acceptance-evidence))
- [x] Reconnect consenting clients explicitly and complete subsequent requests through their new handles. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#acceptance-evidence))
- [x] Fail Disk during a Files call; cancel the chain, recover dependencies in order and perform a later successful read. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#acceptance-evidence))
- [x] Hold device BUSY during owner death and deny replacement submission/regrant until physical quiescence. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#acceptance-evidence))
- [x] Hold BUSY indefinitely and report unavailable/quarantined state while unrelated tasks continue. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#acceptance-evidence))
- [x] Inject failure at every replacement construction/publication step and keep the resolver from advertising a partial instance. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#acceptance-evidence))
- [x] Run a CPU recovery scenario across multiple failure cycles with matching source/image provenance. ([Evidence](../tests/SERVICE_RECOVERY_ACCEPTANCE.md#acceptance-evidence))

## Completion record

Accepted on 2026-10-05. The [acceptance record](../tests/SERVICE_RECOVERY_ACCEPTANCE.md)
maps each item to checked-source, user-policy fixture and CPU evidence, states
physical DMA and persistent-state limitations, and links the
[source/image provenance](../tests/SERVICE_RECOVERY_PROVENANCE.json).
Repeated recovery runs on actual user images across five service generations;
physical indefinite BUSY remains intentionally unavailable and is verified in
source/hardware fixtures, not claimed as successful CPU device recovery.

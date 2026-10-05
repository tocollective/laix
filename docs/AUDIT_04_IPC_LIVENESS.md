# A4. IPC liveness and deadlines

[Audit overview](LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_03_RUNTIME_CAPABILITIES.md) · [Next item](AUDIT_05_SERVICE_RECOVERY.md)

Date: 2026-10-04. Priority: **P1**.

Status: implemented and accepted. See the [liveness contract](IPC_LIVENESS.md)
and [source/CPU acceptance record](../tests/IPC_LIVENESS_ACCEPTANCE.md).
The original gap analysis below is retained as the audit baseline.

## Dependencies

Can start on the current static task model. A1 adds scoped termination and A5 uses deadlines to detect failure and supervise recovery.

## Original state and gap (2026-10-04)

**Evidence:** [ipcCall](../src/ipc/ipc.m) only rejects a direct call to
the caller's own service with EDEADLK. Raw waits and Service AwaitAccept/
AwaitReply have no deadline or user cancellation. Internal cancellation is
used for destruction/termination. [irqWait](../src/drivers/irq.m) has a
timeout, but it does not cover IPC. These restrictions are explicit in the
[request/reply contract](IPC_REQUEST_REPLY.md).

**Consequence:** a live service that stops accepting, loops forever, forgets
a reply or participates in A→B→A leaves clients blocked indefinitely. Timer
preemption keeps unrelated tasks runnable but does not complete those calls.
The scheduler cannot distinguish a legitimate long wait from a stalled service.

**Needed:** add a monotonic deadline facility and timed IPC at least for call,
plus supervisor cancellation/termination. Specify one winning result among
reply, timeout, cancellation and death. Invalidate the reply right and release
the wait pin before publishing Ready. Define nonblocking operations or an
event/wait facility if a supervisor must multiplex completion, IPC and timers.
General cycle detection is optional once waits can be bounded and recovered.

**Acceptance:** test both pre-accept and post-accept timeout, timeout/reply and
timeout/death boundaries, late replies, cyclic calls and an infinite-loop
server. Every client resumes at most once; queues, tokens and references agree.
Document that timeout does not undo server side effects; request IDs and
idempotency rules belong in protocols that support retries.

## Implementation approach

Introduce one wait-completion routine and route timeout, cancellation and reply
through it, rather than adding independent wake paths. The current
`ipcDetach`/`ipcComplete` ownership rules are a useful foundation: unlink,
clear reply authority, release the endpoint pin, store the result, then wake.

A blocked single-threaded caller cannot issue a second cancellation syscall
itself. Initially, timed synchronous calls plus supervisor cancellation are
sufficient. Asynchronous calls or cancellation from another thread require
additional object/thread lifetime rules and can follow separately.

## Implementation checklist

- [x] Define a monotonic clock/deadline representation, comparison rules, overflow policy and maximum supported timeout. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#implementation-evidence))
- [x] Define timed call semantics for both queued AwaitAccept and accepted AwaitReply states. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#implementation-evidence))
- [x] Define whether Raw send/receive and service accept gain deadlines or explicit nonblocking variants in the same milestone. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#implementation-evidence))
- [x] Add per-wait deadline/cancellation metadata to trusted kernel records; user buffers must not hold authoritative wait state. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#implementation-evidence))
- [x] Implement a bounded timer expiry path that detaches the wait, releases its pin and publishes its result exactly once. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#implementation-evidence))
- [x] Invalidate a timed-out accepted reply right before the client resumes, so a late server reply cannot write the old buffer. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#implementation-evidence))
- [x] Add supervisor cancellation under scoped authority; decide whether direct caller cancellation requires another thread or an asynchronous call object. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#implementation-evidence))
- [x] Specify precedence through one serialized completion state machine for reply, expiry, revocation and termination. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#implementation-evidence))
- [x] Preserve FIFO order of unaffected waits when removing an expired/cancelled queue member. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#implementation-evidence))
- [x] Add a sleep/timer event or another bounded waiting interface sufficient for a supervisor watchdog. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#implementation-evidence))
- [x] Define protocol rules for retry IDs, duplicate suppression and non-idempotent side effects. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#implementation-evidence))
- [x] Document that direct self-call detection remains separate from optional general wait-cycle detection. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#implementation-evidence))

## Acceptance checklist

- [x] Time out a call before accept and after accept; both release all wait references and return the chosen timeout error. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#acceptance-evidence))
- [x] Race reply against expiry at the boundary and observe one result, one wake and no stale response write. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#acceptance-evidence))
- [x] Race timeout with server death, client termination and endpoint destruction without duplicate cleanup. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#acceptance-evidence))
- [x] Reject late/repeated replies and prove they cannot complete a later call from the same client. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#acceptance-evidence))
- [x] Test an A-to-B-to-A cycle and a live infinite-loop server; deadlines restore client/supervisor progress. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#acceptance-evidence))
- [x] Expire the oldest and a middle FIFO waiter while unaffected clients retain admission order. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#acceptance-evidence))
- [x] Run sustained CPU cases with real timer entry; mocked tick progression is supporting evidence only. ([Evidence](../tests/IPC_LIVENESS_ACCEPTANCE.md#acceptance-evidence))

## Completion record

Accepted on 2026-10-05. The [acceptance record](../tests/IPC_LIVENESS_ACCEPTANCE.md)
maps every requirement to source checks and CPU cases, states fixture boundaries
and remaining limitations, and links the [source/image provenance](../tests/IPC_LIVENESS_PROVENANCE.json).
CPU acceptance includes sustained natural deadlines with real timer entry;
mocked COUNT tests additionally establish exact rollover and event-order boundaries.
A checked box denotes the evidence level explicitly stated in the linked record.

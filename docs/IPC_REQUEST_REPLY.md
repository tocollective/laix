# Minimal service request/reply contract

Status: implemented; source checks and request/reply CPU acceptance pass
without building. The M accept helper is source-checked; the embedded console uses the raw
accept ABI and executes its public M ABI output helper on CPU. See the
[console contract](CONSOLE_SERVICE.md). Existing syscalls 19/20 remain raw
synchronous message transport. This contract adds an atomic `call` and a
kernel-issued, one-use reply right. See the [acceptance record](../tests/IPC_REQUEST_REPLY_ACCEPTANCE.md).
Timed calls, nonblocking variants, scoped cancellation and watchdog sleep are
implemented by [A4 liveness](IPC_LIVENESS.md). Reply delegation, general cycle
detection and service discovery remain deferred.

## Service endpoints and authority

Trusted bootstrap creates an endpoint in an immutable Service mode and binds
its service task to the endpoint's management owner. Only that task may hold
receive/manage rights; clients receive send rights. Copying receive rights to
another task fails with -EPERM. Send rights may still be copied without
amplification. Existing endpoints retain Raw mode and their existing rules.
There is no user API for creating or rebinding service endpoints in this step.

`call` requires a local send handle; `accept` requires a local receive handle
and the bound service identity. Both require a Live Service endpoint. Raw
`send`/`recv` reject Service endpoints with -EINVAL, and `call`/`accept` reject
Raw endpoints with -EINVAL. This prevents a raw receiver from consuming a
request without obtaining its reply right. Endpoint IDs and payload fields
never authorize either delivery or reply.

The service uses `accept`, processes the bytes and calls `reply`. A client
uses one `call`, remaining Blocked through request delivery until a reply or
cancellation. Two ordinary `send`/`recv` calls cannot provide this guarantee:
their rendezvous completes before a service response exists.

## Syscall ABI

Numbers 21..23 are implemented by `src/trap/trap.m` and `src/ipc/ipc.m`.
`endpointBootstrapService` creates Service endpoints before root issuance is
sealed. [Runtime factories](RUNTIME_OBJECTS.md) can also create fresh Service
endpoints after user scheduling starts, with immutable receiver identity and
separate creator/destruction authority. Normal boot uses the [embedded service bootstrap](BOOTSTRAP.md);
the old Raw endpoint policy remains only as a kernel acceptance fixture.
Every new call preserves r3..r31
and FCSR and advances saved EPC exactly once before any blocking transition.

| Number | Call | Arguments | Success in r1/r2 |
| --- | --- | --- | --- |
| 21 | `call(endpoint, request, length, response, capacity)` | r1: local send token; r2: request VA; r3: request length; r4: response VA; r5: response capacity | response length / response length |
| 22 | `accept(endpoint, request, capacity)` | r1: local receive token; r2: request destination VA; r3: request capacity | request length / positive reply token |
| 23 | `reply(token, response, length)` | r1: reply token; r2: response source VA; r3: response length | response length / response length |

On errors r1 is -errno and r2 is zero, except -EMSGSIZE reports the required
length in r2 when a destination is too small. `accept` returns no reply token
on error. The M `accept` helper in `user/syscalls.m` uses `user/syscalls.asm`
to store both results in an `AcceptResult { length: Word, replyToken: UWord }`.
Its output pointer must name eight writable, word-aligned user bytes, disjoint
from the request destination. The helper sets `replyToken=0` on every error;
the required size on -EMSGSIZE is available only through the raw syscall ABI.
An invalid helper output pointer faults in user mode, invoking normal service
fault cancellation. No token or task identity is taken from the request payload.

```m
import { AcceptResult, accept, reply } from "../user/syscalls.m"

// endpoint and buffers come from the service's trusted bootstrap policy.
let serve(endpoint: UWord, request: *mut UByte, response: *UByte): Word {
    let mut accepted: AcceptResult
    let length: Word = accept(endpoint, request, 32, &mut accepted)
    if length < 0 return length
    return reply(accepted.replyToken, response, 0)
}
```

Requests and responses each have the existing maximum of 32 bytes. Receive
capacities may exceed 32 but their entire ranges must be writable user memory.
Zero-length transfers still validate authority and address-space identity,
without dereferencing the corresponding buffer. Request and response buffers
may overlap: `call` snapshots the complete request into kernel storage before
blocking, and never reads its source VA again. The response VA/capacity stay in
the client TCB and are revalidated through its own directory before reply.

Transport success means delivery, not application success. A service-defined
status belongs in the response bytes; service request types, versions and
status encoding are specified separately by each service.

## Request identity and one-use reply rights

Each non-idle task has one kernel-owned call record and a persistent 23-bit
call generation. A successful admission increments that generation, acquires
one endpoint reference and stores its object generation, request snapshot,
response VA/capacity and bound service ID. Only one IPC wait per task is
allowed, including raw IPC and service calls. No allocation proportional to
untrusted message contents is needed; at most eight task records exist.

On successful `accept`, the kernel grants the bound service a reply right in
that client's record. Its token is `(callGeneration << 8) | clientSlot`,
where the low byte is the diagnostic task slot 1..8, generation is nonzero,
and bit 31 is clear. These tokens are unique across clients and successive
calls. Generations never wrap or reset during a boot; after `0x7FFFFF`, that
task's further `call` admissions fail with -EOVERFLOW (75), without affecting
raw IPC. A future task-slot recycling scheme must preserve the counter or
retire the slot. Endpoint generations are checked independently.

`reply` resolves only an accepted live call record whose reply owner is the
current task. Before copying it checks the exact token, generation, endpoint
liveness/generation, designated service, Blocked client and AwaitReply state.
A malformed, guessed inactive, stale, repeated, or other service's token
returns -EBADF (9), without writing memory or waking any task. Knowing a valid
number does not grant authority. The token cannot select an arbitrary client
buffer or authorize another client's call. A service holding two valid rights
may reply in either order; each token selects exactly its own request.

Reply rights are not handle-table entries. `copyHandle` and `closeHandle`
continue to resolve only local endpoint handles; they cannot copy, close or
delegate a reply right. A coincidentally equal numeric endpoint handle has
only its ordinary local handle meaning. `send`, `recv` and `call` never
interpret their endpoint argument as reply authority.

Successful reply invalidates the right before publishing the client Ready.
The service's copy of the token then has no authority, even if the client
issues a new call before the old token is retried. An accepted right persists
across further `accept` operations, allowing several outstanding clients;
their storage remains bounded by the one-record-per-client rule.

## Wait transitions, validation and errors

All validation, queue mutations, copies, cancellation and scheduler publication
run with IE=0 or EXL=1 on the single CPU. Generic `taskWake` must reject both
service wait states, just as it rejects existing raw IPC waits.

| Client state | Event | Result |
| --- | --- | --- |
| None | valid `call` admission | snapshot request, pin endpoint, become Blocked in AwaitAccept |
| AwaitAccept | successful `accept` | remove from request FIFO, grant reply right, remain Blocked in AwaitReply |
| AwaitReply | valid successful `reply` | copy response, invalidate right, clear wait, drop pin, wake client once |
| Either wait | service revocation | remove queue/right, clear wait, drop pin, wake client once with -EPIPE |
| Either wait | client termination | remove queue/right, clear wait and drop pin without waking the dying client |

`call` validates caller/frame and absence of any existing wait, local handle,
send right, liveness, Service mode, live service identity, self-call rule,
request-size bound, request and full response ranges, then generation and FIFO
capacity. Only after all checks does it snapshot and admit the call. Errors
include -EBADF (9), -EPERM (1), -EPIPE (32), -EINVAL (22), -EDEADLK (35),
-EMSGSIZE (90), -EFAULT (14), -EOVERFLOW (75) and -EBUSY (16), respectively.
No failed admission changes queues, references or generations.

Requests are accepted in FIFO admission order. With no request, `accept`
validates its full destination and blocks, using the same single-wait rule.
An immediate accept whose buffer is too small returns -EMSGSIZE with required
length; an invalid destination returns -EFAULT. Neither consumes the request
or grants a right. A blocked accept is revalidated on arrival; failure wakes
only the service with the same error and leaves the client in AwaitAccept for
a later accept. Both arrival orders must publish the right and AwaitReply
atomically with successful request delivery; the client is never briefly Ready.

`reply` never blocks. It validates caller/frame, reply authority, the 32-byte
size bound and the service's source range, then snapshots the response before
checking the client's full destination range and capacity. An invalid token,
oversized source message or bad service source returns an error only to the
service and preserves the live right, allowing correction and retry.
If an otherwise valid response exceeds the client's capacity, both calls
complete with -EMSGSIZE and the response length in r2; if the client's range
has become invalid, both complete with -EFAULT and r2=0. These destination
failures consume the right and wake the client once, with no partial writes;
a blocked client cannot enlarge its buffer before its `call` returns.

## Self-send and blocking limits

An endpoint's owner is not the destination of a Raw `send`. A task may send
through an endpoint it owns if another task receives there. With one thread
per task it cannot rendezvous with its own later `recv`: a lone raw sender or
receiver stays Blocked until a peer, revocation or kernel cancellation arrives.
Owning both rights is insufficient evidence of deadlock, so raw transport
does not return -EDEADLK merely because the caller owns the endpoint.

A Service endpoint has exactly one designated receiver. After validating
authority/liveness, `call` to a service bound to the calling task returns
-EDEADLK (35), including a zero-length request, without copying or blocking.
A local service function should be invoked directly instead. General cycles
such as A calling B while B calls A are not detected in this step. Legacy
unbounded calls to a live service that never accepts or replies can
still leave a client blocked. Use `callTimed` and scoped supervisor cancellation
from [A4](IPC_LIVENESS.md) to bound these waits.

## Service termination and cancellation

Normal shutdown destroys the Service endpoint before the task exits. Exit,
fatal user fault and kernel termination must revoke every Service endpoint
bound to that task, even if its management handle was previously closed.
Closing its last receive handle also revokes a Service endpoint; closing a
client handle or one of several service receive copies alone does not.
This is a Service-mode rule; Raw endpoint close semantics remain unchanged.

Revocation first marks the endpoint Destroyed, then cancels queued requests,
blocked accepts and **already accepted** calls waiting for replies. Scanning
the bounded task records finds AwaitReply clients no longer in the sender
FIFO. All surviving waiters receive -EPIPE (32), r2=0, exactly once. Each call
keeps its endpoint pin through both wait states; cancellation invalidates its
right, unlinks it and drops the pin before endpoint reuse or client-page reap.
Client exit/fault/kernel termination likewise invalidates any accepted right
before its directory, buffers or TCB can be reclaimed; later reply is -EBADF.

Reply and termination are serialized by IRQ exclusion. A completed reply
remains successful if the service dies afterward; if revocation wins, no
reply writes occur and the client gets -EPIPE. Revocation is not rollback of
service side effects: a client cannot assume that an errored call performed
no output or other operation and must not blindly retry non-idempotent work.
A future restarted service gets a new endpoint/generation; old handles and
reply tokens never address the replacement instance.

## Source and CPU acceptance

- [x] Two clients receive their own replies, including replies in reverse accept order (source checks).
- [x] A foreign task's, stale and repeated reply tokens fail without writes or wakes (source checks).
- [x] Old tokens cannot complete a later call; generation exhaustion rejects admission without reuse (source checks).
- [x] Both arrival orders, FIFO acceptance, zero/maximal messages and overlapping client buffers work under timer-driven scheduler rotations (source checks).
- [x] Failed admission/accept preserves queues; failed service source permits reply retry; client destination errors consume the right once (source checks).
- [x] Service self-call returns -EDEADLK; raw owner-send to a peer still works; a lone raw wait remains cancellable (source checks).
- [x] Destroy, last receive close and service exit/fault/blocked termination cancel AwaitAccept and AwaitReply clients once, even after management close (source checks).
- [x] Client termination revokes its right before reclaim; unrelated clients and scheduling continue normally (source checks).
- [x] Pins, snapshots and wait metadata are cleared on terminal paths; generic wakeups cannot bypass service completion (source checks).
- [x] The M helper captures a successful reply token and clears it on error; returning syscalls preserve registers/EPC (source checks).
- [x] Source checks and CPU acceptance have separate records; no build is used as verification.
- [x] Repeat exchange, rejection, buffer and lifecycle scenarios on a matching
  ready CPU image: ten cases and 128 maximal-message exchanges under timer preemption.
- [ ] Execute the M helper in the first user service image in stage 6;
  the accepted image exercises the kernel ABI through its existing user SYSCALL.

See [stage 5](05_IPC_RIGHTS.md) for existing transport and rights, and
[stage 6](06_USER_SERVICES.md) for the first console service.

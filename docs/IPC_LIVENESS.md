# IPC liveness and deadlines

A4 adds bounded synchronous calls, nonblocking rendezvous, scoped cancellation
and a timer sleep for supervisor watchdogs. Legacy syscalls 19..23 remain
compatible and untimed. See [request/reply](IPC_REQUEST_REPLY.md) for buffer,
handle, FIFO and reply-token rules, and the [acceptance record](../tests/IPC_LIVENESS_ACCEPTANCE.md)
for source and CPU evidence.

## Clock and budget

The monotonic clock is the WRM timer's free-running `COUNT`, represented by
`TimerCount { lo: UWord, hi: UWord }`, two unsigned 32-bit halves. It advances
in virtual CPU clock ticks even with countdown CONTROL disabled or CPU IE
clear. It measures emulated time, not host wall time. A high/low/high read
retries if the high half changed. `FREQUENCY`, with the boot clock fallback,
is fixed for the boot; changing it during a wait is unsupported.

Public timed calls and sleeps take **1..60 whole seconds**. Zero, larger
values and an unavailable clock return `-EINVAL`. No timed syscall interprets
zero as infinity; use legacy `call` for an unbounded wait. The kernel reads
COUNT once and adds the rate up to sixty times with carry, avoiding 32-bit
multiplication overflow. The maximum delta is `60 * 0xFFFFFFFF` ticks, less
than 2^38. Both the counter and addition wrap modulo 2^64. A deadline is
reached when the signed modular difference `now - deadline` is nonnegative;
comparison requires the distance to remain less than 2^63 ticks. These budgets
satisfy that rule, including rollover through zero. There is no saturating
arithmetic or deadline reset at accept.

Timer IRQ acknowledgement precedes expiry scanning and scheduler rotation.
The scan reads COUNT, rather than counting IRQs, so coalesced periods do not
extend a wait. Each IRQ examines at most eight TCBs, with at most eight FIFO
members shifted and 32 snapshot bytes cleared for each completion. The normal
period is 100 Hz. Expiry is observed on the next timer scan; kernel IRQ exclusion
and scheduler load can delay execution and resumption. This API does not
promise a hard wall-time execution bound for kernel work.

## User ABI

Every returning syscall consumes EPC once and preserves r3..r31 and FCSR.
IPC results use r1=result and r2=count/token/required size as before. New APIs
are also exported by `user/syscalls.m`.

| Number | Operation | Arguments | Result |
| --- | --- | --- | --- |
| 55 | `callTimed` | r1 endpoint, r2 request VA, r3 length, r4 response VA, r5 capacity, r6 seconds | response length/count, or `-ETIMEDOUT` (110)/0 |
| 56 | `trySend` | r1 Raw endpoint, r2 source VA, r3 length | count/count, or `-EAGAIN` (11)/0 if no receiver |
| 57 | `tryRecv` | r1 Raw endpoint, r2 destination VA, r3 capacity | count/count, or `-EAGAIN`/0 if no sender |
| 58 | `tryAccept` | r1 Service endpoint, r2 request destination VA, r3 capacity | request length/reply token, or `-EAGAIN`/0 if no request |
| 59 | `cancelTaskWait` | r1 generation-bearing task reference | 0 on cancellation, `-EPERM` without scoped authority, `-ESRCH` for a dead target, `-EAGAIN` if no cancellable wait |
| 60 | `sleep` | r1 seconds | 0/0 after expiry, or `-ECANCELED` (125)/0 after supervisor cancellation |

Nonblocking operations retain all authority/mode/range checks and FIFO rules.
They never publish a wait or pin for the caller. A too-small `tryRecv` or
`tryAccept` leaves the oldest request pending and reports `-EMSGSIZE`/required
size. `trySend` may complete existing incompatible blocked receivers with the
ordinary receive error while searching for a receiver able to take the message.
If none succeeds it discards its snapshot and returns `-EAGAIN`.
`tryAccept` uses the same `AcceptResult` assembly shim contract as `accept`:
the helper retains a token only on success and sets it to zero on errors.
Raw waits and blocking accept do not gain deadlines in this milestone; use
the explicit try variants and bounded sleep where a bounded wait is required.

## One serialized completion

An admitted timed call stores a deadline and flag in its kernel TCB. The user
request/response contains no authoritative timer or cancellation state. The
same absolute deadline covers queued AwaitAccept and accepted AwaitReply.
Accept removes the FIFO entry and installs the reply right while preserving
the pin, deadline and Blocked state.

Reply, timer expiry, supervisor cancellation, endpoint revocation and task
termination run under the single CPU's IE=0/EXL=1 exclusion. The **first
serialized terminal transition wins**. Crossing the counter deadline alone
does not preempt an already executing reply handler; a reply entering before
the expiry handler may win even if COUNT reaches the deadline during that
atomic handler. Conversely, once the expiry handler consumes the record, a
reply is stale. This is an observation deadline, not a strict rejection of
all replies whose completion COUNT is at or past the deadline.

`ipcFinishWait` consumes a blocked record once: unlink its FIFO member if
queued, clear reply owner/buffer/capacity and endpoint identity, release the
wait pin, clear deadline metadata, store the result, then publish Ready.
Reply validates and copies under the same exclusion before this terminal
transition. The resulting errors are `-ETIMEDOUT` for expiry, `-ECANCELED` for
supervisor cancellation, and `-EPIPE` for revocation/service loss. Client
termination consumes the same record without waking the dying task. A task
already completed may subsequently die; termination then removes Ready
membership rather than completing the old wait again.

Late/repeated reply tokens return `-EBADF` before reading or writing the old
response. A later call increments the persistent call generation, so the old
token cannot target it. FIFO removal shifts only the removed entry's successors;
unaffected waits keep admission order. Generic `taskWake` cannot bypass an
IPC wait or a timed sleep.

## Supervisor progress and authority

`TASK_RIGHT_CANCEL=32` is independent of `TASK_RIGHT_TERMINATE=8`.
`TASK_RIGHT_ALL=63` grants six rights for a newly created child. Bootstrap
can grant cancellation for specific targets; numeric task references alone
do not authorize it. Cancellation affects the target's **current** synchronous
IPC wait (Raw send/receive, call in either state, or accept) or timer sleep.
It does not cancel IRQ/DMA operations, terminate a task, roll back a request,
or select a historical wait. `-EAGAIN` is an expected race when that wait has
already completed. A dead or reused task reference cannot identify a new
lifetime, and existing ownership checks remain mandatory.

One thread per task means a blocked caller cannot invoke cancellation itself.
There is no asynchronous call object or second thread in this milestone.
Use timed calls or a separately scheduled supervisor holding scoped control.
Existing scoped termination can stop a live CPU loop after cancellation; the
normal service revocation and resource-reclamation rules then apply.

A watchdog can poll `inspectTask`/`collectTask` and `tryAccept`/`tryRecv`, then
`sleep(1)` before polling again. Sleep needs no IRQ capability and returns
through the timer even when no user task is runnable. Completion mailboxes
and queued IPC requests persist across the poll/sleep window. Detection latency
includes the sleep interval and scheduling. This supplies bounded waiting
without an event multiplexer; applications needing tighter latency or atomic
multi-source waiting require a later event facility.

Direct Service self-call still returns `-EDEADLK` independently of timeout.
General A-to-B-to-A detection remains optional and unimplemented. Timed calls
bound every participating wait; protocol/supervisor policy decides recovery.

## Protocol retries and side effects

Timeout or cancellation does **not** undo effects already performed by a
service, nor prove that it never accepted the request. A service may continue
processing the copied request after its reply right expires. Do not blindly
retry non-idempotent output, writes or transactions.

A retry-capable protocol must define a logical request ID, client/session
identity and service incarnation. Reuse the same logical ID for retries of
one operation, and assign a new ID for a new operation. Bind ID to the request
content; reject reuse with different content. The kernel reply token and call
generation are delivery authority, not durable protocol request IDs.

The service must specify duplicate suppression, result retention and bounded
cache eviction. Replay a retained result for a duplicate instead of repeating
a side effect. After eviction, reject/reconcile an old ID rather than silently
executing it again. Exactly-once claims across crashes require durable result
and side-effect coordination; a volatile duplicate cache alone cannot provide
them. A restarted incarnation must either restore that state or make uncertainty
explicit. A status/query operation or user reconciliation is needed when the
outcome is unknown. Idempotent operations should document why repeating them
is safe and how conflicting versions are handled. These are requirements for
protocols that choose retries, not a new generic kernel retry mechanism.

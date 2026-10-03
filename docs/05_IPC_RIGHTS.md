# Этап 5. IPC и права на объекты

[План ядра](KERNEL.md) · [Предыдущий этап](04_SCHEDULER_IRQ.md) · [Следующий этап](06_USER_SERVICES.md)

Status: endpoint objects, task-local handles, synchronous send/receive and
FIFO waits are implemented and checked from source without building. The
minimal service request/reply contract passes source checks and ten CPU cases
on a matching ready image. Standalone Raw transport CPU acceptance remains
pending; see the [request/reply acceptance record](../tests/IPC_REQUEST_REPLY_ACCEPTANCE.md).

## Начальный контракт

Для первого IPC достаточно синхронных send/recv с фиксированным верхним
пределом размера, например 32 байта. Если другой стороны пока нет,
вызывающая задача блокируется. Произвольные очереди больших сообщений,
разделяемая память и передача capabilities могут быть следующими улучшениями.

Предлагаемые вызовы: `send(endpoint, buffer, length)` и
`recv(endpoint, buffer, capacity)`. Номера, формат сообщения, максимальную
длину, коды ошибок и результат нужно зафиксировать в ABI LA/IX.
Большой результат нельзя молча вернуть в r3–r6: текущий syscall ABI
предусматривает результаты r1/r2 и сохранение остальных регистров.

## Что сделать

### Objects and handles

- [x] Define endpoint identity/generation, state, sender/receiver queues,
  reference count, and management owner.
- [x] Give every task its own handle table with an object reference,
  object generation, handle generation, and send/receive/manage rights.
- [x] Resolve handles and check rights in the calling task's table.
  A global numeric object ID alone grants no authority.
- [x] Define handle close, endpoint destruction, and copying without increasing rights.
- [x] Reject stale handles after slot reuse and retire exhausted generation slots.
- [x] Grant initial authority exclusively through trusted bootstrap code.

#### Implemented capability contract

`src/ipc/objects.m` owns a fixed pool of 16 endpoints. Each `Task` embeds a
separate 16-entry `HandleTable` in supervisor memory. An endpoint has a stable
pool ID, a 32-bit generation, Empty/Live/Destroyed/Retired state, a reference
count, and the task ID of its management owner. Sender and receiver FIFO
storage each holds eight trusted task IDs with a head and count. References
count installed handles and queued IPC waits. Each task can own only one wait,
and each wait pins its endpoint independently of the handle used to enter it.

A handle entry stores an endpoint pointer, the captured endpoint generation,
its own generation, and a rights mask. `RIGHT_SEND=1`, `RIGHT_RECEIVE=2`, and
`RIGHT_MANAGE=4`; valid masks are nonempty subsets of 7. User tokens encode
`(handleGeneration << 8) | (slotIndex + 1)`. Bits 0..7 hold a slot in 1..16;
bits 8..30 hold a nonzero generation. Zero, bit 31, invalid slots, missing
entries, mismatched handle/object generations, insufficient rights, and non-Live
objects cannot authorize access. Tokens are positive signed syscall results.

`src/ipc/ipc.m:ipcResolve` uses only `currentTask.handles` and a nonzero required
rights mask. It never indexes the global object pool using user input. Tokens
are scoped to a task, not secret. Equal tokens in different tasks may identify
different objects; presenting another task's number only looks up the caller's
own entry, if one exists. Kernel object/table pointers never cross the user ABI.
All lookup, copy, close, destruction, and task cleanup operations require IE=0
or EXL=1 on this single CPU. Borrowed endpoint pointers remain valid only inside
that exclusion region. IRQ-enabled calls panic before reading the caller's table.

Copying installs a new independent reference in the target task's table.
Requested rights must be a nonempty subset of the source entry's rights; a
recipient cannot amplify them by copying again. A target task ID selects a
recipient, not an object authority. Management copies are permitted only within
the original owner's task. Ownership transfer is not implemented. Nonmanagement
rights can be copied to any live user task, including the caller. Copy returns
the new token in the recipient's table; delivering that number to the recipient
is a separate protocol. A failed copy does not change either table or references.

Closing removes exactly one table entry. Other copies remain usable while the
endpoint is Live. Closing the last reference releases the object automatically.
A manager may close its last management handle while peer references remain;
the endpoint remains Live, but no new management authority can be minted.
`destroyEndpoint` requires both manage rights and the original owner's identity.
It marks the object Destroyed immediately, revoking every copy. Destroyed entries
can still be closed, but cannot be copied or used. They pin the object slot until
the last reference closes, so a new endpoint cannot reuse it prematurely.
Exit and fatal user faults release all of the task's handles in `taskFinish`
before scheduler selection. Manager death destroys all its Live endpoints,
even if it previously closed its management handles. Death of a nonmanager
only drops that task's references. Task slots currently are not recycled.

Handle generations advance on every allocation and are preserved on close and
task cleanup. A closed slot at generation `0x7FFFFF` is permanently unavailable.
Endpoint generations advance on each pool allocation; a released endpoint at
`0xFFFFFFFF` becomes Retired permanently. Neither generation wraps to zero.
If all usable slots are busy or retired, allocation returns the same bounded
capacity error: `-EMFILE` for a handle table or `-ENFILE` for the endpoint pool.

Trusted `taskBootstrapEndpoints` grants task 1 send/receive/manage and task 2
send on a shared endpoint before scheduling starts. Each initial token is placed
in r4 of that task's private entry frame. Tasks created without this policy have
empty tables and r4=0. The existing user demo does not consume r4 yet.
`taskStart` permanently seals root issuance before the first user IRET. There is
no user endpoint-create syscall and no later API for minting root rights.

| Syscall | Arguments in saved r1..r3 | Result in r1 |
| --- | --- | --- |
| 16 `closeHandle` | local token | 0 or negative errno |
| 17 `copyHandle` | local token, target task ID, subset rights | positive target token or negative errno |
| 18 `destroyEndpoint` | local token | 0 or negative errno |

Each syscall consumes EPC once and preserves r2..r31 and FCSR. User wrappers
are in `user/syscalls.m`; rights constants can be imported from
`src/arch/wrm081632/defs.m`. Error policy: `-EBADF` (9) for an invalid local token;
`-EINVAL` (22) for an invalid rights mask; `-EPERM` (1) for increased rights or
unauthorized management; `-EPIPE` (32) for a destroyed endpoint; `-ESRCH` (3)
for an absent, Empty, Dead, or idle copy recipient; `-EMFILE` (24) and
`-ENFILE` (23) for capacity exhaustion. Checks run in this order: copy validates
the recipient, source token, mask, subset, liveness, management owner, and capacity;
destruction validates the token, manage right, liveness, and owner.

`tests/test_ipc_handles.py` executes checked M ASTs for private table isolation,
rights attenuation, management restrictions, reference lifetime, revocation,
slot reuse, both generation limits, capacity and bootstrap rollback, IRQ
exclusion, root sealing, syscall register preservation, and exit/fault cleanup.
These are source checks, not CPU acceptance of a newly built image.

### Message transfer and waits

- [x] Check task-local rights, size and the entire user-buffer range before changing queues.
- [x] Copy outgoing bytes to kernel-owned TCB storage before blocking.
- [x] Publish a single FIFO wait and Blocked state with IRQs excluded.
- [x] Deliver one message and complete each participating wait exactly once.
- [x] Translate the destination through the receiver's directory and checked physical aliases.
- [x] Return a verified byte count or error; preserve messages on insufficient capacity.
- [x] Treat all payload fields as untrusted; authorize access through kernel-owned handles.
- [x] Reject duplicate waits and prevent generic wakeups from bypassing IPC completion.
- [x] Cancel waits and release references before endpoint or task resources can be reclaimed.

#### Implemented transport contract

| Syscall | Arguments in saved r1..r3 | Results |
| --- | --- | --- |
| 19 `send` | local token, source VA, byte length | r1=delivered length or -errno; r2=delivered length on success, otherwise 0 |
| 20 `recv` | local token, destination VA, byte capacity | r1=received length or -errno; r2=length on success or required length on -EMSGSIZE, otherwise 0 |

The maximum message length is `IPC_MESSAGE_MAX=32`. Receive capacity may exceed
32, but its **entire** range must be writable user memory. Zero-length messages
still rendezvous, require a live authorized handle and valid address-space
identity, but never dereference either buffer; their addresses may be arbitrary.
Only r1/r2 change; r3..r31 and FCSR are preserved. The dispatcher advances EPC
once before blocking, and completion resumes after that same instruction.
M wrappers return r1; a caller receiving -EMSGSIZE can retry with capacity 32.
The raw syscall ABI also exposes the required length in r2.

Validation order is running caller/frame, absence of an existing wait, local
handle, send/receive right, endpoint liveness, send-size bound, and complete
buffer validation. Invalid tokens return -EBADF (9), insufficient rights return
-EPERM (1), destroyed endpoints return -EPIPE (32), invalid ranges/permissions
return -EFAULT (14), and send lengths above 32 return -EMSGSIZE (90).
Duplicate waits or exhausted wait storage return -EBUSY (16). No endpoint queue
or reference changes occur on these validation failures.

`send` snapshots bytes with `copyFromUser` into `Task.ipcMessage`. A blocked
sender retains only that snapshot and its verified length, never the source VA.
`recv` retains a destination VA and capacity in its TCB; delivery revalidates
the capacity and calls `copyToUser` with the **receiver's** directory and task
ID. The current sender's PTBR need not change. Both helpers validate all pages
before copying through supervisor physical aliases. IRQ exclusion remains in
force across validation, queue changes, delivery and scheduler transitions;
there is no mapping change or lost-wakeup window between these operations.

Each side uses FIFO order. If no peer is waiting, the current task appends its
trusted ID, acquires one endpoint reference and becomes Blocked atomically.
When a receiving caller is too small, -EMSGSIZE and the oldest message's length
are returned without writing, dequeueing or waking the sender. When a sending
caller encounters a small blocked receiver, that receiver wakes with -EMSGSIZE;
the message is offered to the next FIFO receiver, or the sender blocks with its
snapshot intact. A blocked receiver whose capacity is no longer writable wakes
with -EFAULT under the same rule. At most one eligible receiver receives the
message. No partial copy counts as success, and no newer sender overtakes an
older pending message. Successful completion detaches the wait, clears its
buffer metadata and snapshot, drops its pin and enqueues the waiter once.

Payload bytes have no kernel-defined sender identity. An embedded task ID is
untrusted data, never evidence of identity. A protocol needing authentication
must use controlled endpoint rights or add explicit kernel-issued metadata.
The transport does not implement reply rights or authenticate payload fields.
A lone task can wait on an endpoint it owns, including for an empty message;
without another task, revocation or kernel cancellation it stays Blocked.
There is no automatic self-delivery and no timeout.

Endpoint destruction and manager exit/fault revoke the endpoint first and wake
all queued calls with -EPIPE, releasing their wait pins. `taskFinish` cancels
waits and releases handles before selecting another task. The kernel-only
`taskAbortBlocked` handles termination of suspended tasks, removes any queue
membership, marks them Dead and releases handles before the normal inactive
resource reaper. A blocked task cannot execute a user exit or fault itself.
Before rendezvous there is no assigned peer: terminating a nonmanager removes
only its own wait and handles; unrelated FIFO waiters stay queued. Manager
termination also revokes its endpoints and fails all peers. After rendezvous
both calls have completed, so later task death cannot undo the transfer.
Task slots are not recycled, and wait references prevent endpoint reuse while
any TCB still points at it. Ordinary `taskWake` rejects an active IPC wait;
only IPC completion may detach it and publish Ready.

`tests/test_ipc_transport.py` executes the real checked transport, scheduling,
MMU validation and byte-copy source together. It covers both arrival orders,
FIFO queues, exact wake counts, snapshots surviving source changes, full-range
and cross-page validation, equal VAs in different roots, zero/maximal messages,
small-buffer retry, receive revalidation, revocation, owner exit/fault, blocked
sender/receiver termination, reference release, idle wakeup and repeated
exchanges with timer-driven scheduler rotations. These checks generate no code
and do not replace CPU acceptance of a built image.

### Service request and reply

- [x] Define the minimal protocol: atomic `call`, service `accept`, and a
  restricted one-use `reply` right instead of a public response endpoint.
- [x] Define kernel-owned request identity, generation and service binding;
  reject foreign, stale and repeated replies without writes or wakeups.
- [x] Document raw owner-send and lone waits; reject a Service self-call with
  -EDEADLK before blocking.
- [x] Define cancellation of queued and accepted calls on service termination
  or revocation; defer timeouts until the base exchange works.

The normative [minimal request/reply contract](IPC_REQUEST_REPLY.md) defines
Service endpoints, syscalls 21..23, reply token lifetime, error and
buffer rules, shutdown races, and acceptance scenarios. Existing raw
`send`/`recv` still have no reply identity or automatic self-deadlock check.

- [x] Implement the contract and an M service helper that retains the reply token.
- [x] Pass its source checks and record CPU acceptance status separately in
  [the request/reply acceptance record](../tests/IPC_REQUEST_REPLY_ACCEPTANCE.md).
- [x] Pass request/reply CPU acceptance on a matching ready image: ten cases,
  474 IPC syscalls and 128 maximal-message exchanges under timer preemption.

## Что проверить

- [x] Both arrival orders transfer the same message (source checks).
- [x] Waiting tasks become Blocked without polling (source checks).
- [x] Multiple senders/receivers obey FIFO without duplicate delivery or wakeups (source checks).
- [x] Reject foreign, closed, and stale handles and insufficient rights (source checks).
- [x] Copying cannot increase rights; closing one copy preserves the documented
  lifetime of remaining references (source checks).
- [x] Invalid pointers, page boundaries, overflowing lengths, oversized messages
  and small buffers leave memory/queues intact (source checks).
- [x] Equal VAs with different physical data copy into the receiver's memory (source checks).
- [x] Task termination removes its wait; endpoint revocation fails all peers
  and releases references/pages through normal teardown (source checks).
- [x] Two clients receive their own replies; foreign and repeated reply tokens
  are rejected (source checks and CPU acceptance).
- [x] 128 maximal-message request/reply exchanges pass under CPU timer
  preemption without lost messages or wait references.
- [x] Service exit/fault and suspended task termination cancel IPC waits;
  a third task continues receiving timer quanta and issuing syscalls on CPU.

## Когда этап готов

Две реальные user-задачи в разных каталогах обмениваются сообщениями,
ожидание экономит CPU, права проверяются, а все пути ошибки завершаются
без зависших задач и повреждения памяти.
Общий контракт регистров — [syscall ABI](../../docs/ABI.md#system-calls).

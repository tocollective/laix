# Limits and execution latency contract

A8 implementation, 2026-10-05. [Acceptance and provenance](../tests/LIMITS_LATENCY_ACCEPTANCE.md).
These are single-CPU, one-thread-per-task limits. Pool capacity, per-domain
charges and lifetime identity exhaustion are separate constraints.

## Concurrent resources and domain charges

| Resource | Concurrent limit | Domain charge and failure |
|---|---|---|
| User tasks | 8, including Created/Blocked/Dead until collection/reaping | Ordinary creation uses six slots; two are recovery-reserved. Four child/control records per ordinary creator, including uncollected children. `ENFILE` on admission failure |
| Task control/completion rows | 16 | Two reserved for recovery; completion requires no allocation |
| Endpoints | 16 | Factory-issued quota at most 12, two recovery-reserved rows; `ENFILE` |
| Handles | 16 per task | Two recovery-manager installation slots reserved; `EMFILE`; foreign installation requires consent |
| IPC waits | One per task; eight entries per endpoint FIFO | Caller owns its wait and endpoint pin; seven other user tasks can precede a caller; queue admission returns `EBUSY` |
| IPC payload and destination capacity | 0–32 bytes | `EMSGSIZE` for any greater send/request/reply length or receive/accept/response capacity, before walking user buffers; r2=0 for oversized admission |
| Physical pages | Installed usable RAM, with 16 pages reserved | 96 frames per task, including directory/tables, startup, stacks and guards; borrowed frames charged to the lender; `ENOMEM` |
| Ordinary user mappings | 128 per address space, including aliases, startup and grants | An alias spends a leaf charge even when it allocates no frame; mapping returns `ENOMEM` at quota |
| Private user page tables | 8 per address space | Includes bootstrap device tables; two-table region publication is rejected atomically if only one table charge remains; `ENOMEM` |
| Address-space budget ledger | 32 roots | Public task construction has at most eight live roots; trusted internal root construction also fails closed at ledger exhaustion |
| Memory authority rows / regions / grants | 32 / 64 / 64 | Four authority rows, eight allocation regions and eight outgoing grants per domain; `ENFILE` |
| Region operations | 1–16 pages (64 KiB) | Allocate/map/unmap/protect/grant operate on one bounded region; loader populate is at most the region's 64 KiB, checked before any copy |
| Device operations | One selected storage engine, one pinned sector operation | 1–512 bytes within one approved sector; immutable owner/instance; `EBUSY` prevents overlapping submission |
| Input batch | 1–32 raw events; 8-byte header | At most 136 bytes validated/copied; `EINVAL` outside capacity |
| IRQ ownership | One exclusive owner per line | No generation reset on owner death; pending levels stay masked until acknowledged/rearmed |
| Service discovery | Eight rows, four names per supervisor namespace | Consent installs only into caller's table; status/completion storage is preallocated |

Bootstrap Screen has three exclusive, fixed resource descriptors. Their device
leaves do not spend ordinary RAM mapping charges: each descriptor spans at most
one 1024-leaf table. Their tables do spend the eight-table quota. Kernel identity
mappings are shared; runtime callers cannot request arbitrary MMIO/superpages.
Orphan grant allocations and BUSY DMA retain their original charge/pins until
safe release; creating another task does not forgive those physical costs.

## Lifetime identities and recovery

| Identity | Last valid generation | Exhaustion policy |
|---|---|---|
| Reply identity, per physical task slot | `0x7fffff` (8,388,607 admitted calls) | Final call may complete; every later call returns `EOVERFLOW` without queue/pin/counter mutation; collected slot is retired from task construction |
| Task reference | `0x7fffff`, starting at generation zero | Construction reservations, including failures, consume generations; exhausted slots are never selected again |
| Handle slot / receiver transfer ticket / IRQ line | `0x7fffff` | Persist across task/owner reuse; retired handles yield `EMFILE`, transfer tickets `EOVERFLOW`, IRQ grant fails without wrapping |
| Memory authority / region / grant row | `0x7fffff` | Persistent row generations; skip exhausted rows and return `ENFILE` when no usable quota/row remains |
| Endpoint row | `0xffffffff` | Last object may live and release; row then becomes Retired, never Free |
| Storage extent / device operation | `0x7fffffff` | Regrant/update or new operation returns `EOVERFLOW`; completion/cancellation of an existing operation remains possible |
| Published service resource generation | `0x7fffffff` | Supervisor must publish a strictly increasing positive generation; invalid/repeated generations return `EINVAL`. Supervisor death releases its namespace, whose full task identity is different on replacement |

The chosen RPC policy is safe retirement and supervised replacement, keeping the
existing ABI. A long-lived application must treat `EOVERFLOW` as a terminal
client-generation event: stop admitting requests, exit or be terminated through
its supervisor's control, wait for physical reaping, collect its completion,
construct/configure a replacement and explicitly reconnect its handles. At the
reply limit the kernel selects a different physical slot. Replacement before
that limit can reuse a slot, but preserves its cumulative reply counter.
Outstanding old waits are cancelled before Ready/death and old reply tokens
remain invalid, even if a server keeps their numeric values.

Replacement is an explicit application/supervisor action, not transparent RPC
retry or state migration. At-most-once application execution is not inferred
from transport completion. This policy has a finite **boot lifetime**: eight
reply namespaces admit at most 67,108,856 calls combined; ordinary factories
cannot use the two reserved slots. Handle, IRQ and other row retirements can
force maintenance sooner. If all eligible namespaces retire, creation returns
`ENFILE` without allocations. An indefinite-lifetime deployment requires a new
wider ABI or a planned whole-system restart after all applications and waits
have ended. Tokens are boot-local authority, never persistent reconnect keys.
No runtime operation resets a counter to recover capacity.

## Syscall work and scheduling admission

Every public syscall selects fixed ledgers or first bounds its variable span.
The dispatcher has no user-supplied loop count outside these contracts:

| Syscall family | Admitted work under IRQ exclusion |
|---|---|
| Debug / yield / exit / task control | One byte, one fixed TCB/context, or fixed eight-task/sixteen-control scans; exit revokes fixed handles/waits/grants |
| Raw and Service IPC, try operations, timed calls | At most 32 bytes per copy, at most two user pages per buffer, fixed eight-wait FIFO removal/cancellation; timed deadline addition at most 60 iterations |
| Endpoint/handle/transfer/discovery | Fixed 8/16-entry scans; one accepted transfer/notification per task; no recursive graph walks |
| Memory authority/allocation/mapping/edit | Fixed 32/64-entry ledgers; at most 16 page allocations/edits; 16-page mapping stages at most two absent tables before publication |
| Populate | At most 64 KiB, at most 17 source pages if unaligned, at most 16 destination pages; complete validation before first write |
| Grant creation/map/close | At most 16 frames per region, fixed 64-grant ledger; explicit borrower consent and alias charge |
| Device/Input/IRQ | One sector/136-byte snapshot, one operation, fixed PIC lines/eight tasks; BUSY returns or quarantines rather than polling indefinitely |
| Runtime approved image creation | Immutable catalog: at most three PT_LOAD entries, 64 image pages (256 KiB), bounded byte copying and creation rollback; untrusted callers select catalog IDs, not arbitrary image lengths |
| Deferred reaping | At most eight dead tasks; each root scans 1024 directory entries and at most eight private user tables of 1024 leaves. Ordinary leaves are limited to 128, with three bounded resource descriptors; 64-region/grant cleanup ledgers |

Allocator scans are bounded by installed frames (8192 at 32 MiB), and task frame
quota/recovery reserve checks precede allocation. The creation-time batch APIs
are trusted helpers, not syscalls accepting arbitrary counts. Future larger
buffers, image catalogs, memory sizes, tables or bulk graph teardown require
new measurements. Work exceeding the accepted section budget must be split into
stages with resources pinned/unpublished until commit, cancellation between
stages and no partial mapping or freed-resource exposure. No staged operation
is introduced here: current maximum fixtures fit the section budget.

CPU admission is preemptive FIFO round-robin at 100 Hz, one runnable entry per
task and one thread per domain. There is no priority, CPU reservation, donation
or hard real-time guarantee. A CPU-bound domain gets one quantum before another
ready domain, subject to IRQ-excluded kernel work. TIMER expiry is coalescing,
not elapsed-time CPU billing. Creating extra tasks spends the supervisor's child
and global task budgets; unlimited identities cannot defeat this policy.

A shared Service admits one outstanding call per client task, with FIFO accept
order. The server never waits for a client to consume a response: reply copies
at most 32 bytes to a checked kernel-owned wait, or fails/completes it. A held,
invalid-buffer or unresponsive client can occupy only its own record; the
server may accept and serve other callers. This is a bounded concurrency/FIFO
policy, not a requests-per-second reservation. The server's user protocol must
bound per-request work; services in the supported image process fixed 32-byte
requests, at most 16 data bytes per read and 32 Input events. Use `callTimed`
(1–60 seconds) and supervisory cancellation for failure-sensitive chains.
A deliberately looping **server** needs watchdog replacement; FIFO alone does
not impose a server application execution deadline.

## Timing envelope and retained correctness baseline

Measurements use the existing WRM binary and preserved ROM, at **128 MHz**,
with **32 MiB** for maximum memory/reaping and **2 MiB** for Input/Disk services.
The accepted images and exact sizes/hashes appear in provenance. The section
budget is **64,000,000 virtual CPU cycles (500 ms)**, with timer progress within
that budget plus a 1,280,000-cycle quantum. Device completion uses the selected
service's five-second IRQ deadline plus that section budget. These are measured
regression budgets for the stated images/workloads, not a proof of a global
worst-case bound for all possible mappings or a host wall-clock guarantee.
The hardware/allocator accepts up to 128 MiB; 32–128 MiB has no A8 latency
acceptance. The Screen 2 MiB requirement from A7 remains in effect.

[The acceptance record](../tests/LIMITS_LATENCY_ACCEPTANCE.md) separates logical
AST tests from actual CPU counters. Samples cover trap entry through the final
IRET, including selected-stack reaping; the IRET instruction itself and interrupt
delivery before the entry breakpoint are outside that span. API-only setup
fixtures are excluded. Context switches retain full TLB invalidation, and their
cost is measured with timer IRQs and IPC before any ASID optimization.

There are no cached ASID leases. Every activation fences, invalidates all
translations before PTBR publication and preserves the existing stale-address
probes. Any future caching must define full-generation leases, invalidation on
mapping edits/teardown and reuse only after stale-translation CPU probes pass.
Trap entry remains single-core and nonnested; kernel preemption must not be
turned on without replacing the static entry scratch protocol and providing
complete nesting-safe context/stack/fault handling.

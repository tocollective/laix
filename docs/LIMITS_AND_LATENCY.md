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
existing ABI (G4 decision: no wider reply identity, see the
[operating limit](#operating-limit-and-planned-maintenance)). A long-lived application must treat `EOVERFLOW` as a terminal
client-generation event: stop admitting requests, exit or be terminated through
its supervisor's control, wait for physical reaping, collect its completion,
construct/configure a replacement and explicitly reconnect its handles. At the
reply limit the kernel selects a different physical slot. Construction
chooses namespaces with more than `LIFETIME_REPLY_RESERVE` (4096) calls left
before any namespace at or below it, so replacing a client that is within the
reserve lands in a fresh namespace when one exists. Only when no other eligible
slot remains does it reuse the nearly spent one, which preserves its
cumulative reply counter.
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

## Remaining-lifetime report (G4)

`SYS_LIFETIME` (75) is read-only: it advances and resets nothing. It needs the
caller's creation authority (a catalog image bit). A nonzero reference also
needs INSPECT on that child, until the child is collected; a reaped but
uncollected child returns `-ESRCH`. It writes one `LifetimeReport` (eleven
words, 44 bytes) to a user buffer, with `-EFAULT` before any state change.
"Remaining" means admissions or constructions still possible; zero is retired.

| Field | Meaning |
|---|---|
| `bytes`, `limit` | Report size; last valid 23-bit generation (`0x7fffff`) |
| `replySelected` | Admitted calls left in the selected child's reply namespace (0 without a reference) |
| `replyTotal`, `replyOpen` | Calls left and number of namespaces the caller can still construct into. Ordinary callers see six, recovery-class callers eight |
| `taskSelected` | Task-reference generations left in the selected child's slot |
| `handleSelected` | Fewest handle-slot generations left in the selected child's table |
| `retiredTasks` | Slots with an exhausted reply counter or task-reference counter |
| `retiredHandles`, `retiredEndpoints`, `retiredIrqs` | Rows that can never be allocated again |

User wrapper `lifetimeReport`; policy helpers `lifetimeDue` and
`retireClient` are in `user/recovery/policy.m`. `lifetimeDue(reference, reserve)`
is true when `replySelected <= reserve`. Nothing in the kernel prints a warning:
a supervisor reads the report and tells its operator.

## Operating limit and planned maintenance

**Decision (G4): the 23-bit reply identity is kept; the written limit below is
the contract.** A wider identity would change the token layout `generation << 8
| slot`, which fits one positive 32-bit result, and needs a versioned ABI and
migration of every user helper. The [target workload](TARGET_WORKLOAD.md) is
sessions of hours with a planned restart accepted, which the limit below fits
except under saturated back-to-back calling. Revisit it if a deployment must
run longer than that without a restart.

| Limit | Value |
|---|---|
| Calls per reply namespace | 8,388,607 |
| Calls per boot, ordinary supervisor (6 namespaces) | 50,331,642 |
| Calls per boot, with the two recovery slots (8) | 67,108,856 |
| Task references per slot | 8,388,608 (generation zero through `0x7fffff`) |

Arithmetic only, not measured rates: at a sustained 100 admitted calls per
second one namespace lasts about 23 hours and an ordinary supervisor's six
about 5.8 days; at 1,000 calls per second, 2.3 and 14 hours; at the roughly
2,300 calls per second of back-to-back calling at the A8 maxima (the
[target workload](TARGET_WORKLOAD.md) estimate), one hour and about six hours.
Retirement of a
handle, endpoint or IRQ row can end a boot sooner, and `retired*` shows it.

Planned replacement of one client, before it can see `EOVERFLOW`:

1. Read `lifetimeReport(client)`. When `replySelected <= LIFETIME_REPLY_RESERVE`
   the client is due. Alert the operator at a larger margin if one is wanted.
2. Stop sending the client work. An application that must not lose a request
   lets it finish first; termination cancels any outstanding wait and does not
   migrate application state.
3. `retireClient(client)`: terminate, wait up to five seconds for reclaim,
   collect. Close the supervisor's own handles for it.
4. Construct, configure and publish the replacement, and reconnect its
   handles explicitly. The replacement has a new reference and a new reply
   namespace; the old tokens stay invalid.
5. Read the replacement. When it is still due, no fresh namespace is left:
   stop replacing (each construction spends a task-reference generation) and
   schedule the whole-system procedure.

Whole-system restart: stop admitting work, drain or terminate every
application, collect completions, confirm no wait or DMA pin remains, then
reset the machine. Tokens are boot-local; applications reconnect from scratch.
Do not attempt to reset a counter at runtime. The source evidence is
[test_lifetime](../tests/test_lifetime.py); the CPU scenario ran locally and
passed ([acceptance record](../tests/LIFETIME_ACCEPTANCE.md)).

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
| Runtime image load (`SYS_TASK_LOAD`) | One copy of at most 64 KiB into a private snapshot in at most 16 borrowed free frames (the reserve is kept), the catalog's image checks, then the catalog's bounded construction with rollback. Measured at most 1,859,714 cycles (14.5 ms) for the 38,788-byte test image ([Files-backed loading](FILES_LOADER.md)) |
| Lifetime report | Fixed scans: eight task slots, sixteen handle slots in each, sixteen endpoint rows and 32 IRQ lines; one 44-byte copy. Task construction scans the eight slots twice |
| Deferred reaping | At most `TASK_REAP_STAGE_TASKS` (one) dead root per section, so a section is one root: 1024 directory entries and at most eight private user tables of 1024 leaves. Ordinary leaves are limited to 128, with three bounded resource descriptors; 64-region/grant cleanup ledgers. The other dead tasks wait for a later stage ([staged teardown](#staged-teardown-g5)) |

Allocator scans are bounded by installed frames (8192 at 32 MiB), and task frame
quota/recovery reserve checks precede allocation. The creation-time batch APIs
are trusted helpers, not syscalls accepting arbitrary counts. Future larger
buffers, image catalogs, memory sizes, tables or bulk graph teardown require
new measurements. Work exceeding the accepted section budget must be split into
stages with resources pinned/unpublished until commit, cancellation between
stages and no partial mapping or freed-resource exposure. Task reaping is the
one staged operation (G5, below); populate, load and mapping stay single
sections that fit the budget.

## Staged teardown (G5)

Reaping used to tear down every dead task in one IRQ-excluded section, so the
interrupt latency of an eight-task exit was the sum of eight roots (12,144,655
cycles, 94.9 ms, measured 2026-10-05). `taskReap` is now a staged, resumable
operation:

- One call commits at most `TASK_REAP_STAGE_TASKS` (one) dead root and returns
  whether another reapable task waits. A section is therefore one root, about
  1.55 million cycles, not eight.
- **Pinned until commit.** A dead task keeps its root, mapped frames, kernel
  stack, charges and slot until its own stage commits `reaped`; there is no
  half-released state to observe between stages. Task construction cannot pick
  the slot, and the stage does not run on the dying task's stack.
- **Resumption.** Every trap return that selects a task runs one stage on the
  selected stack. The idle loop runs one stage per poll and, while more roots
  wait, opens one IRQ window and polls again instead of sleeping
  (`taskIdlePoll` returns `TASK_IDLE_STAGE`; the assembly takes the
  `.selected` branch). The window is where a timer or device IRQ is serviced
  between stages, so interrupt latency is one stage, not the whole teardown.
- **Cancellation and arrival.** Stages run in slot order. A task killed while
  teardown is in flight simply joins the pending set; a collection or second
  termination between stages sees the completion record without the
  `RECLAIMED` flag until its own stage ran. A task with a BUSY DMA operation is
  skipped without spending the stage and without keeping the idle loop awake.
- **Visible effect.** Reclamation of a second and later dead task is delayed by
  one stage per task ahead of it: next trap return or idle poll, 10 ms at worst
  under a 100 Hz timer with a running task. `retireClient` already polls for
  `RECLAIMED`; a supervisor that needs the frames must too.

Granularity is one root. A root is not split further because the largest other
sections (populate 64 KiB, 13.7 ms; a full `SYS_TASK_LOAD`, 14.5 ms) are of the
same size, so a per-table stage would not lower the budget.

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
budget is **2,560,000 virtual CPU cycles (20 ms)** since G5 (2026-10-08); the
A8 baseline was 64,000,000 cycles (500 ms), when one section tore down eight
roots. Timer progress is checked within the section budget plus a
1,280,000-cycle quantum (3,840,000 cycles). Device completion uses the selected
service's five-second IRQ deadline plus that section budget. The probe also holds
a per-kind ceiling, about 1.5 times the measured maximum, for the staged reap,
EXIT, populate, unmap, allocate, map, timer IRQ and the IPC calls
(`KIND_CEILINGS` in [probe_limits_latency_cpu.py](../tests/probe_limits_latency_cpu.py)),
so one operation growing cannot hide under the shared budget. These are measured
regression budgets for the stated images/workloads, not a proof of a global
worst-case bound for all possible mappings or a host wall-clock guarantee.

| Section (G5 run, local) | Maximum cycles | At 128 MHz |
|---|---:|---:|
| One staged reap on the idle stack (7 samples) | 1,550,591 | 12.11 ms |
| Final EXIT, which also commits one root | 1,555,589 | 12.15 ms |
| Populate 64 KiB | 1,753,847 | 13.70 ms |
| `SYS_TASK_LOAD`, full load ([Files loader](FILES_LOADER.md)) | 1,859,714 | 14.53 ms |
| Longest timer-to-timer gap | 3,177,135 | 24.82 ms |

The A8 figure for the same fixture, EXIT plus all eight roots in one section,
was 12,144,655 cycles (94.9 ms) with a 12,224,903-cycle timer gap. The longest
section fell from 12,144,655 to 1,859,714 cycles (the full load), 6.5 times.

The hardware/allocator accepts up to 128 MiB (four 32 MiB slots). The
maximum memory/reaping workload (`probe_limits_latency_cpu.py`) was also run on
the changed tree at **128 MiB** (G1 and again for G5, 2026-10-08, local): the
kernel's own `kernelRamEnd` read 134,217,728 bytes with 32,342 free frames, and
every section maximum equalled the 32 MiB run to the cycle, including each reap
stage (1,550,591 cycles) and the timer gap (3,177,135 cycles). The
allocator is next-fit from a cursor, so those fixtures never walk far. A worst
case that does (a full, fragmented 128 MiB) scans at most 32,768 frames per
call; that bound is arithmetic, not a measurement. Input/Disk services and
Screen were not run at 128 MiB. The Screen 2 MiB requirement from A7 remains
in effect.

[The acceptance record](../tests/LIMITS_LATENCY_ACCEPTANCE.md) separates logical
AST tests from actual CPU counters. Samples cover trap entry through the final
IRET, including the selected-stack reap stage; an idle stage is measured from the
idle loop's `mtcr status, r0` to its stage-pending branch or WFI. The IRET
instruction itself and interrupt delivery before the entry breakpoint are
outside that span. API-only setup
fixtures are excluded. Context switches retain full TLB invalidation, and their
cost is measured with timer IRQs and IPC before any ASID optimization.

There are no cached ASID leases. Every activation fences, invalidates all
translations before PTBR publication and preserves the existing stale-address
probes. Any future caching must define full-generation leases, invalidation on
mapping edits/teardown and reuse only after stale-translation CPU probes pass.
Trap entry remains single-core and nonnested; kernel preemption must not be
turned on without replacing the static entry scratch protocol and providing
complete nesting-safe context/stack/fault handling. G5 decided that no selected
workload needs it ([TARGET_WORKLOAD](TARGET_WORKLOAD.md)): with the longest
section at 14.5 ms, a nesting-safe protocol would buy interrupt latency the
profile does not ask for, at the price of a new, unverified entry path.

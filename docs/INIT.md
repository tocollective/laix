# Init, the root server

Status: the product. There is one kernel image (`build.sh`); the session init runs
is chosen by data (`LAIX_SESSION`). Source accepted; CPU pending (the shell session
has been booted by hand). The reference boots that the source tests and the CPU
campaign still use live in [tests/programs/boot](../tests/programs/boot)
(see [Fixtures](#fixtures)).

The kernel no longer has to know which services a system consists of. It
starts exactly one trusted user task, **init**, and gives it the authority to
build everything else. This replaced the per-profile boot policy
(`*_bootstrap.m`, `service_policy.m`), which is why the kernel used to be built
in many variants.

## Boot

`kernelInit` → `initBootstrap` ([init_bootstrap.m](../src/kernel/init_bootstrap.m))
→ `taskStart`. `initBootstrap` constructs one task from the embedded ELF
`initImage`, registers the image catalog, grants the root authority below and
publishes it. Nothing else is created by the kernel.

## Root authority

Only init holds these; they are Task fields no syscall can set on another task.

| Authority | Meaning |
| --- | --- |
| `createImages` = catalog mask \| `IMAGE_LOAD_AUTHORITY` | Create any catalog image; load an ELF read from storage |
| `deviceFactory` = every device bit | Ask the device broker for UART TX, keyboard, Disk, font storage, display and Ethernet grants for an unpublished child |
| Endpoint factory, recovery quota | Create Raw and Service endpoints; use the reserved task slots and endpoint rows |
| Self control, service registry | Inspect and terminate its own children; supervised publication and resolution |

The catalog is the list of `{start, end}` rows in
[init_bootstrap.asm](../src/kernel/init_bootstrap.asm). Row *n* is image *n*+2
(image 1 is the kernel's self-test fixture); [images.m](../user/init/images.m)
names them for init, and a test keeps the two lists in the same order.
`IMAGE_CATALOG_MAX` is 31 and `IMAGE_LOAD_AUTHORITY` is bit 31.

## What init does with it

Everything goes through runtime calls a supervisor already had, plus three
added for this model. Each acts only on an unpublished child that the caller
controls (`TASK_RIGHT_CONFIGURE`), and each fails without side effects.

| Call | Purpose |
| --- | --- |
| `SYS_TASK_HANDLES` (81) `reference, entries, count` | The start handle list: up to 6 `{token, rights}` pairs. With rights, an attenuated copy of one of the caller's handles is installed in the child; with rights 0 the word is stored as is (an interrupt token). All entries are checked first and a failed copy closes the earlier ones |
| `SYS_TASK_SERVICE_START` (82) `reference, role, protocol, endpoint, upstream, irq` | The checked service start record used by Disk, Fs, Input and the Screen services, built from the caller's handles and the child's device grant. The same cross-checks as the boot-time installer apply, including upstream manager and interrupt token |
| `SYS_TASK_AUTHORITY` (83) `reference, images` | Narrowing delegation of image authority: only bits the caller holds. This is how Exec gets `IMAGE_LOAD_AUTHORITY` |

Also changed for init: `SYS_TASK_DEVICES` accepts `DEVICE_NET`
(exclusive, like Disk and Screen); `SYS_DEVICE_EXTENT` treats zero bytes as
"the rest of the approved root" so a manager that does not know the root size
can still grant it; `SYS_TASK_PUBLISH` understands both start record layouts.

## Sessions

A session is the graph init builds: [session.m](../user/init/session.m) holds one
function per session, written as the straight line of its wiring with the helpers of
[lib.m](../user/init/lib.m) (each step records the first failure, so a session
checks once). `init.m` dispatches on its start argument; an unknown number is an
error, never the default.

| Session | Number | Graph |
| --- | --- | --- |
| `shell` | 0 | Disk (writable) ← Fs ← Exec, Shell; Console ← Exec, Shell; keyboard to Shell |
| `console` | 1 | Console ← Banner |
| `services` | 2 | Input, Disk (read-only) ← Files ← Application (and Input) |
| `loader` | 3 | The same; the client may load programs it reads from Files |
| `fs` | 4 | Input, Disk (writable) ← Fs ← client (and Input) |
| `net` | 5 | Net driver ← IP ← client; Console ← client |
| `screen` | 6 | Bitmap storage ← Screen ← Application |
| `recovery` | 7 | Init is the supervisor of Echo, Disk and Files (user/recovery) |
| `screenrecovery` | 8 | Init is the supervisor of Echo, bitmap storage and Screen |

**Selection.** The number is defined once, in [sessions.m](../user/init/sessions.m).
`build.sh` reads it through `tools/sessions.py`, writes it into bits 8..15 of the
storage root flags (`storage_root.py --session`), and the kernel hands it to init as
its start argument. The kernel does not interpret it. A session also decides the
volume: a WFS1 image for `shell` and `fs`, the program for `loader`, the font for
the rest.

**Order and failure.** A server is spawned before its clients and published first;
device grants precede start records; Disk gets its extent before Fs's record names
it as upstream. If any step fails, every child created so far is terminated or
discarded and init exits with code 2. After publication init supervises the session
as a unit: a member that should keep running (not a one-shot application) ending
stops the rest and init exits with 10 + the member's index, so a half-running
system is never left. The recovery sessions supervise per service instead
(restart, backoff, explicit reconnect), as the old supervisors did.

**Catalog.** [images.m](../user/init/images.m) names the images of
[init_bootstrap.asm](../src/kernel/init_bootstrap.asm) (images 2..25 today, at most
31); `tests/test_init.py` keeps the asm rows, the constants and the build list in
the same order. Programs on the volume (the shell's `hello`, `count`, `spin`, the
loader's child) are not catalog images.

## Checking a session

[test_sessions.py](../tests/test_sessions.py) runs the real thing: `initBootstrap`
and the kernel's source evaluator, with init's own M code (user/init) in a second
evaluator whose `syscall` builtin enters the kernel as init's trap
([init_harness.py](../tests/init_harness.py); pointer arguments cross by copy
through init's data page). The session builds its graph through the runtime
syscalls, so every kernel cross-check (start records, upstream managers, device
grants, interrupt tokens, exclusive extents) applies, and the test then checks who
holds which authority and handle. It stops at init's first sleep, when the graph is
built and published, and also covers the failures (a read-only root refuses the
write window and leaves no child; an unknown session is an error).

## Task slots

The number of tasks is not a constant. Every table that has one entry per task
slot is carved out of RAM at boot ([tables.m](../src/task/tables.m)), sized from
the installed memory, so a small machine gets a few dozen slots and a 128 MiB
machine about 3,600. Nothing sized by the task count lives in BSS.

| Per-slot table | Where it is carved | One entry is |
| --- | --- | --- |
| Task records, ready ring | `taskTableBind` | 680-byte TCB; one reference |
| Transfer rows | `transferTableBind` | receiver-selected endpoint transfer |
| Task control / completion rows | `taskControlTableBind` | one child, or one uncollected completion |
| Allocator budgets | `memoryBudgetBind` | frame quota of a task |
| Address-space ledger | `mmuInit` | mapping and page-table quota of a root |
| Endpoint wait queues | `endpointQueuesBind` | two queues of one entry per slot in each of the 16 endpoints |

**How many.** `memorySlots` ([memory.m](../src/mm/memory.m)) budgets nine frames
per task, the least a task can hold (directory, tables, three task pages, a
guarded kernel stack), with a minimum of eight and a ceiling of 4,095. The
tables cost about 1.1 KiB a slot, roughly one frame in thirty-three of the RAM
they stand for, and are one zero-filled run of frames owned by a reserved kernel
identity. If no such run exists the count is halved until one does.

**The ceiling.** A task reference is `(generation << 12) | slot`; reply tokens
and transfer tickets use the same layout, and all of them travel as positive
31-bit results. That leaves 4,095 slots and a 19-bit generation
(`TASK_GENERATION_MAX`, 524,287 lifetimes per slot and as many admitted calls
per reply namespace, down from 23 bits). The slot also picks the hardware ASID,
modulo 256; every activation flushes the TLB, so ASIDs are never relied on to be
unique. The ceiling is the reference layout, not RAM: 128 MiB is budgeted 3,623
slots. Changing `TASK_SLOT_BITS` moves the ceiling and the generation range
together.

**Scans.** Every loop over tasks, transfer rows or completion rows stops at a
high-water mark (`taskHighWater`, `taskControlHigh`): a record beyond it has
never been used and is still all zero. Lowest-free-slot allocation keeps the
marks near the number of tasks that really existed.

Init occupies one slot, so a graph of five services plus Exec's programs no
longer exhausts the ordinary slots. The limits that are *not* per task are
unchanged and are now what bounds a system first: 16 endpoints, 16 handles per
task, 32 memory spaces, 64 regions and 64 grants, 8 service-registry rows, and
4 live children per ordinary creator (init is exempt as a recovery supervisor).
The IRQ-excluded latency budget was measured with 8 slots; a CPU run must
measure it again. The CPU probes were adapted mechanically to the pointer
symbols (`probe_boot.Symbols`) and have not been run.

## Fixtures

The old per-profile boots remain as test fixtures, not as part of the product:
[tests/programs/boot](../tests/programs/boot) holds the kernel entries that place
a fixed task graph without init (`uart`, `screen`, `services`, `loader`, `fs`,
`shell`, `net`, `recovery`, `screenrecovery`, `supervisor`, `soak`) and the
`memory`, `sharing` and `objects` stands. `tools/build_fixture.sh`
(`LAIX_FIXTURE=<name>`) builds them, the acceptance bundle uses it, and the source
tests of the kernel mechanisms (DMA, input, loader, recovery) still build their
graphs with them. They are the same kernel with a different entry; what they check
keeps its layout (task 1 is the first service, not init). `LAIX_FIXTURE=shell sh
tools/build_fixture.sh` writes `build/shell.img`; run it with `LAIX_IMAGE=shell sh run.sh`.

What is *not* moved yet: the CPU probes of the product sessions. The probes read
specific task slots and states of the fixture graphs, so the production variants
`recovery-production`, `screenrecovery-production` and `screenrecovery-watchdog`
are no longer built (their supervisors are init's recovery sessions) and need a
probe re-based on a session image before they return to the campaign. The stress
mains (`uart-stress`, `screen-stress`, `hid`) and the kernel-mode stands (`latency`,
MMU, trap, stack) are unchanged fixtures.

## Evidence

Source accepted: [test_init.py](../tests/test_init.py) (authority held only by init,
catalog and session numbering, the three calls and their refusals, rollback),
[test_sessions.py](../tests/test_sessions.py) (every session built by init's own
code against the real kernel) and [test_task_tables.py](../tests/test_task_tables.py)
(capacity follows RAM, the 4,095 ceiling, one owned zeroed run bound once, a task in
slot 300 with a full reference). The whole source suite, 633 tests, passes. No image was built
and no CPU probe was run: the pilot session has never booted, and every earlier
CPU result predates the changed kernel bytes.

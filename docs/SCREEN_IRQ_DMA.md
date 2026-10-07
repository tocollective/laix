# Screen services, IRQ delivery and DMA authority

The eight screen/IRQ/DMA implementation items in [stage 6](06_USER_SERVICES.md)
are implemented. The default [UART console](CONSOLE_SERVICE.md) retains its
protocol and emergency debug path. A separate screen boot starts a user screen
server, bitmap storage server and application. Source and CPU evidence, with
the limits of each check, is recorded in
[screen acceptance](../tests/SCREEN_IRQ_DMA_ACCEPTANCE.md).

From the repository root:

```sh
LAIX_CONSOLE=screen sh laix/build.sh
LAIX_CONSOLE=screen sh laix/run.sh
```

This builds LA/IX and its embedded user images, using the existing compiler,
emulator and firmware. It does not build WRM or firmware. Screen artifacts are
`build/screen.img`, `build/screen.map` and `build/obj/screen/`; default UART
artifacts remain `build/laix.img`, `build/laix.map` and `build/obj/uart/`.

## Components and startup

`src/kernel/screen_main.m` initializes the kernel and calls trusted
`service_bootstrap.m`. The screen task owns a receive-only screen endpoint,
a send-only bitmap endpoint, exclusive VRAM, read-only video registers and
video IRQ 5. Storage owns its receive-only endpoint and the selected boot
disk IRQ (3, 4 or 6). The application holds only a screen send handle.
No client-supplied address or task ID grants authority.

`tools/build_services.sh` links three independent user ELF images. The limited
kernel-only constructor in `task/program.m` accepts these trusted embedded
images: bounded, disjoint PT_LOAD segments with RX, R or RW permissions, a
validated executable entry, zero-filled BSS and rollback. It is not a general
user ELF loader. Each task has private code/data, guarded stacks and a root.
Bootstrap publishes tasks only after all construction and grants succeed;
construction and grants are sealed before user entry.

The pointer-free `ServiceStart` ABI is version 2, 64 bytes, in the existing
RO/NX startup page. Its first twelve words retain the startup layout; four
additional words contain bitmap endpoint, font-index VA, font-index byte
length and generation-bearing IRQ token. Roles and protocol IDs are checked
against actual endpoint rights and resource mappings before installation.
Screen startup uses protocol 3; bitmap startup uses protocol 4. UART uses
its original version-1 startup record and protocol 2.

The user modules in `user/screen/` divide responsibilities:

| Module | Responsibility |
| --- | --- |
| `server.m`, `client.m` | Bounded screen request/reply and complete-request serialization |
| `unicode.m`, `font.m` | Strict bounded UTF-8 decoding, validated LAF1 index and Unicode lookup |
| `cache.m` | 256-slot FIFO glyph cache; staged bitmap replies and cache-hit validation |
| `video.m` | CPU rendering, clear, scroll, VRAM upload and notification-based frame wait |
| `storage.m` | Bitmap request validation and replies; waits for broker DMA through IRQ notification |
| `application.m` | Ordinary banner and Unicode output through the screen endpoint |

These user closures have no kernel allocator, disk register programming,
supervisor UART, privileged trap callback or arbitrary DMA operation.
The legacy `src/console/` path remains available for supervisor regression
fixtures; neither normal UART boot nor screen boot initializes that path.
Emergency kernel UART remains independent of services, IPC, disk and user RAM.

## Screen protocol

Screen writes use wire version 2, distinct from the UART version-1 protocol.
All fields are little-endian; the transport limit is 32 bytes.

| Message | Fields, in order | Size |
| --- | --- | --- |
| Request | u8 version=2, u8 type=1, u8 text length=0..28, u8 reserved=0; UTF-8 text without terminator | Exactly 4 + length |
| Response | u8 version=2, u8 type=2, u8 payload length=8, u8 reserved=0; i32 status, u32 completed text bytes | 12 |

The server validates the entire delivered message before rendering: exact
header/size, complete UTF-8, no overlong encoding, surrogates or values above
U+10FFFF. Printable scalars and TAB/LF/CR are accepted; other C0 controls,
DEL and C1 controls are rejected. There is no shared decoder state across
requests. Unsupported valid scalars use the validated fallback glyph.

Invalid messages/text return `-EINVAL` and zero completed bytes; a declared
text length above 28 returns `-EMSGSIZE`. Success returns the exact byte
length. Rendering/storage errors propagate a negative errno and count only
complete UTF-8 sequences already rendered. A final frame-wait error may
report the full rendered count. A hardware failure disables further output
for this server instance; subsequent valid requests return `-EIO`.

One accept/render/reply loop serializes complete requests in FIFO kernel
call-admission order. The client `screenWrite` validates response header,
size, status and count; it returns the count or a negative transport/service
error, and `-EPROTO` for an inconsistent response. It does not split requests.

## Device grants and every mapping

The kernel resource ledger records exact owner/root, physical and virtual
range, and permissions. `mmuGrantResource` accepts only the following fixed
bootstrap grants, creates private page tables and fences/invalidates the TLB.
No ordinary RAM mapping/protection/copy API can create or extend a grant.

| Resource | Screen user mapping | Other user tasks | Supervisor alias |
| --- | --- | --- | --- |
| Framebuffer and glyph-cache VRAM | `0x80000000`, 77 pages, RW/U/NX | None | RW/NX |
| Video register page | `0x80400000`, one page, R/U/NX | None | RW/NX |
| Immutable embedded font index | `0x80800000`, page-rounded index, R/U/NX | None | R/NX |
| Disk, PIC, timer and other MMIO | None | None | Supervisor/NX |

The VRAM extent is exactly 640*480 + 256*32 bytes. The font index is
page-aligned, separately padded, validated and shared read-only to avoid a
duplicate 457 KB allocation at 1 MiB RAM. Resource installation rejects X,
wrong addresses/identity, conflicting ownership, extra pages and overflow.
Startup validation checks the ledger against every actual resource PTE.
Inherited device superpages never gain U or X. IPC user-buffer copies accept
private allocator RAM only, including when the task can access VRAM itself.

Rollback and directory destruction validate resource leaves, remove their
ledger entries and apply FENCE/TLBI. VRAM and reserved font frames are never
freed as allocator pages. Task death revokes its aliases through ordinary
directory teardown and retires its device policy and IRQ grant.

Video COMMAND, ADDRESS and COUNT share a page with safe control registers;
therefore a writable video-MMIO grant would expose physical-memory DMA.
The screen's read-only alias cannot issue commands. `screenControl` permits
only four fixed operations: 0 configures the 640x480/8bpp mode, palette and
cursor; 1 enables scanout and VBLANK IRQ; 2 clears both DONE and VBLANK with
W1C and fence; 3 stops scanout/IRQs. There is no user drawing-engine command.
All clear, scroll, bitmap expansion and uploads use CPU stores into granted
VRAM, followed by a fence.

## IRQ ownership, notification and rearm

WRM PIC is level-triggered. CLAIM reads the lowest enabled pending line;
it neither consumes an event nor acknowledges the device. There is no PIC
EOI. The scheduler owns timer IRQ 2. Each selected device line has one owner
and a kernel generation-bearing token: `(generation << 8) | (line + 1)`.
Users cannot write ENABLE or install a function pointer.

The kernel record tracks owner, generation, pending, in-service state,
the selected line's waiting flag and an optional deadline. A notification
means inspect device state; repeated levels coalesce rather than becoming an
event count. Once in service, the same line cannot publish another notification
until successful rearm, including after a blocked wait consumed the first one.

1. Bootstrap grants the line masked. Initial rearm requires cleared device
   state. The broker adopts and acknowledges the boot medium once; video
   initialization clears both video causes before arming.
2. The ordinary trap prologue saves context. Device IRQ dispatch masks and
   fences the non-timer line before publishing pending or waking its owner.
   It never calls a user PC, dereferences a user buffer or waits for disk.
   Unowned lines remain masked, preventing repeated unserviced entry.
3. `irqWait` authenticates owner/generation, consumes existing pending or
   atomically blocks under EXL with `WAIT_IRQ`. Pending survives Ready,
   Running and unrelated IPC waits. Only a wait for this exact line is awakened;
   another line owned by the same task retains its own pending notification.
   Waking
   supervisor idle selects a Ready task through `taskIrqReturn`; IRQ EPC
   is never advanced. An optional timeout is 1..60 seconds; zero is unlimited.
   The screen/storage services use five seconds. Expiry masks the line and
   wakes with `-ETIMEDOUT`.
4. The service uses the broker to inspect/finish and clear device causes,
   with fences before completing. Video DONE and VBLANK are cleared together.
   Disk DONE is cleared after capturing completion/error/media status.
   A new floppy CHANGED permanently invalidates this boot font generation;
   storage replies with an error and exits, leaving its line masked rather
   than accepting replacement media.
5. `irqComplete` accepts only the owner/generation with an in-service record
   and consumed notification. If PIC_PENDING is still high, it returns
   `-EBUSY` and stays masked. Otherwise it rearms while preserving unrelated
   ENABLE bits. A new level between the check and enable remains pending
   in the level-triggered PIC. No automatic unserviced redelivery occurs.

Owner death masks the line, cancels its wait and retires its grant. Old or
foreign tokens return `-EPERM`. The bootstrap grants of this fixed boot are
sealed and are never regranted; a restartable Screen is the separate
[supervised display](SERVICE_RECOVERY.md#supervised-display-screen-and-bitmap-storage).

| Syscall | Operation |
| --- | --- |
| 24 | IRQ wait(token, optional timeout seconds) |
| 25 | IRQ complete(token) |
| 26 | Screen control(fixed operation) |
| 27 | Font begin(glyph ID, half 0 or 1) |
| 28 | Font finish(private user destination for 16 bytes) |
| 29 | Font cancel/revoke current generation |
| 30 | Font validate current medium |

Both task device rights and the broker's exact selected owner are checked.
Applications receive none of these operations.

## DMA trust and buffer lifetime

WRM DMA uses physical addresses and bypasses the CPU MMU. Full disk MMIO,
video STORE/LOAD or memory-source EXPAND, and unrestricted network/audio
registers make a driver a trusted system component. CPU page isolation alone
cannot contain their device writes. The screen and bitmap services are
untrusted: neither receives unrestricted DMA MMIO.

The narrow trusted kernel broker in `drivers/service_devices.m` fixes the
boot device and immutable font-sector extent. `fontBegin` accepts only a
validated glyph ID and half 0/1. It allocates a page-aligned PAGE_KERNEL
bounce page with broker owner `0xFFFFFFFE`, checks physical ownership, retains
an allocator pin, and zeros the 512-byte transfer area. The page's physical
address is stored explicitly; no user request or response pointer supplies
the DMA address. A single READ of one checked font sector is the only disk
command. WRITE, IDENTIFY, lists, arbitrary sectors/devices/addresses and
command flags have no interface. One active reservation serializes the disk.

Register writes and buffer initialization are fenced before command
publication. `fontFinish` requires DONE with BUSY clear, fences, captures
error and validates medium identity before checked copying of exactly
16 bytes into storage's private user RAM. Partial/error transfers publish
no bitmap. DONE is acknowledged and fenced before releasing the pin and
freeing the page. Allocator references forbid free/reuse while DMA is live.

Timeout, cancellation, owner death and IRQ masking are not completion.
WRM disk DMA cannot be stopped except by reset. Cancellation revokes the
font generation, abandons the kernel-owned reservation and permits no new
commands. The page outlives task-directory teardown. `fontReap` retains it
while BUSY is set; only observed quiescence with a fence permits release.
The task reaper also defers final DMA-owner directory/page/stack destruction
until this quiescence check succeeds. If quiescence never occurs, the page and
owner resources remain quarantined until machine reset. See the
[simple-service lifecycle](SIMPLE_SERVICES.md#dma-service-destruction).
No unfinished data is exposed by this cleanup path.

## Bitmap protocol and cache

The bitmap endpoint uses wire version 1 and immutable boot generation 1.
No message contains a disk offset, physical address, device selector or
user pointer. The screen maps Unicode to glyph IDs using the shared index.

| Message | Fields, in order | Size |
| --- | --- | --- |
| Read request | u8 version=1, u8 type=1, u8 payload length=12, u8 reserved=0; u32 generation, u32 glyph ID, u32 half (0 or 1) | 16 |
| Validate request | Same layout, type=3; glyph ID and half must be zero | 16 |
| Response | u8 version=1, u8 type=2, u8 payload length=28, u8 reserved=0; i32 status, u32 generation, u32 count; 16 bitmap bytes | 32 |

Exact sizes/headers are mandatory. Read success returns 16 bytes; validate
success returns zero count/data and issues no DMA. Failure returns zero
count/data. Malformed messages/IDs/halves return `-EINVAL`; stale generation
or changed/absent medium returns `-EPIPE`; device/partial/timeout failure
returns `-EIO`. Busy/allocation errors and transport cancellation propagate.
Storage returns terminal EIO/EPIPE before exiting; endpoint teardown cancels
later calls. The screen rejects malformed, mixed-generation or failed replies.

Each glyph is 32 bytes; sixteen share a 512-byte sector. The broker checks
glyph count before deriving `firstBitmapSector + glyph / 16`, validates the
extent, and copies at `(glyph % 16)*32 + half*16`. Both halves are staged in
private RAM. Only after both succeed does the cache upload/fence and publish
a VRAM slot; failure preserves the old slot. Each half currently reads one
sector; there is no storage sector cache. Cache hits send a validate request
so PRESENT/CHANGED invalidation is observed without disk MMIO or DMA rights.
A replacement medium is never accepted under the old generation.

The legacy supervisor `cacheGlyph` writes a virtual pointer to disk ADDRESS;
it is physical only in the kernel identity window. That operation remains
restricted to its old regression fixture. The migrated user cache obtains
bitmap bytes through IPC and never programs a DMA address.

Hardware references: [memory map](../../docs/SPECIFICATION.md#memory-map),
[PIC](../../docs/SPECIFICATION.md#pic),
[disks](../../docs/SPECIFICATION.md#disks),
[video engine](../../docs/SPECIFICATION.md#drawing-engine) and
[VRAM](../../docs/SPECIFICATION.md#vram-window).

# Simple services and failure lifecycle

The optional `services` profile starts four independent user images in order:
keyboard input, read-only disk, immutable file service and an application.
The existing UART and screen profiles remain available. This milestone keeps
networking, general user ELF loading and a full filesystem deferred.

```sh
LAIX_CONSOLE=services sh laix/build.sh
LAIX_CONSOLE=services sh laix/run.sh --ram 2M --no-net
python3 laix/tests/probe_simple_services_cpu.py \
    laix/build/services.img laix/build/services.map
```

The build compiles LA/IX and its user images using the existing M toolchain.
The run and CPU probe use an existing emulator and ROM; neither builds WRM.
Artifacts are `build/services.img`, `build/services.map`, and independent
`build/services/{input,disk,files,simple-application}.elf` images. CPU acceptance
uses 2 MiB RAM. The [acceptance record](../tests/SIMPLE_SERVICES_ACCEPTANCE.md)
distinguishes source evaluation from CPU/device evidence.

## Authority and startup

The kernel constructs all tasks, endpoints, private address spaces and startup
records before publishing any task. Every image has RX code, immutable R data,
private RW data, guarded user/kernel stacks and a RO/NX `ServiceStart` v2 page.
No new service has a user-accessible MMIO or DMA register mapping. A mapping
failure rolls back all four tasks and their capabilities; retry advances handle,
object and IRQ generations rather than clearing them.

| Task | Startup role / protocol | Capabilities | Device operations |
| --- | --- | --- | --- |
| Input | 4 / 5 | Receive-only input endpoint, keyboard IRQ 0 token | `DEVICE_INPUT=8`: bounded keyboard snapshot |
| Disk | 5 / 6 | Receive-only disk endpoint, selected boot disk IRQ token | `DEVICE_DISK=16`: extent info, bounded begin/finish/cancel |
| Files | 6 / 7 | Receive-only file endpoint, send-only disk endpoint | None |
| Application | 2 / 7 | Send-only file and input endpoints | None |

The first twelve startup words retain the existing layout. For these protocols,
word 12 (`bitmapEndpoint` in the v2 struct) is the auxiliary send endpoint:
disk for Files, input for Application, zero for Input and Disk. Font fields are
zero. IRQ is present only for Input and Disk. Installation authenticates exact
receive/send rights, endpoint management owners, IRQ line/owner/generation and
the upstream task's installed role. No user-supplied task ID creates authority.
The bootstrap and construction APIs are sealed before the first user entry.

Syscalls 31..35 are respectively input snapshot, disk extent info, disk begin,
disk finish and disk cancel. Every operation checks both the task's exact device
right and the broker's selected owner. Other tasks receive `-EPERM`. The disk
broker shares the pinned buffer machinery used by the font service, but is
selected in a separate boot profile and cannot compete with a live font owner.

## Input protocol

All protocols below use little-endian words, exact header/size validation and
the existing maximum 32-byte IPC message. The first header word contains
u8 version=1, u8 type, u8 payload length, u8 reserved=0.

| Message | Words after header | Bytes |
| --- | --- | --- |
| Poll, type 1, payload 0 | None | 4 |
| Result, type 2, payload 28 | i32 status, u32 event count, u32 flags, four u32 HID events | 32 |

A successful poll returns 0..4 events in FIFO order; no events is success with
count zero. Each event contains USB HID usage page 0x07 in bits 0..15 and a
release flag in bit 31. Flags bit 0 reports dropped events; other bits and
unused event words are zero. Invalid messages return `-EINVAL`, zero count,
flags and events. Public `inputEvents` validates the whole response before
publishing any event or flag.

The kernel checks all 24 output bytes before reading the hardware's
read-to-clear overflow status or popping events. It drains at most 32 hardware
events into a bounded 32-event ring, preserving order and recording either
hardware or software overflow until a successful snapshot. When the ring is
full, newly drained events are dropped; existing events remain ordered. It
returns at most four events and zero padding. Invalid destinations consume no
hardware event or overflow flag.

IRQ notifications coalesce while the user server is in IPC accept. A snapshot
masks the line, services FIFO/overflow, consumes the notification and rearms
only after checking the actual PIC level. If arrivals leave the FIFO nonempty,
the line stays masked until the next poll; the readable events remain queued.
The v1 client protocol is nonblocking event polling. There is no key-repeat,
layout translation or blocking event subscription. The server blocks in IPC
accept between requests. Death masks/retires its IRQ token, flushes the FIFO,
clears buffered events and destroys its endpoint; clients receive `-EPIPE`.

## Read-only disk protocol

The only readable extent is the immutable bitmap area appended to this boot
image, starting at the boot image's sector count and spanning
`fontGlyphCount * 32` bytes. Offsets are relative to that extent. The client cannot
choose a device, physical buffer, boot/kernel sector, command flags or write.

| Message | Words after header | Bytes |
| --- | --- | --- |
| Read, type 1, payload 12 | u32 generation=1, u32 byte offset, u32 count=1..16 | 16 |
| Stat, type 3, payload 12 | u32 generation=1, zero, zero | 16 |
| Result, type 2, payload 28 | i32 status, u32 generation=1, u32 count, 16 data bytes | 32 |

Stat returns the extent's byte length in count and zero data; it performs no
DMA. Read returns exactly the requested count, with unused bytes zero. The
broker validates count, offset, extent subtraction, sector boundary and drive
sector range before issuing a single-sector READ into a page-aligned, pinned,
kernel-owned bounce page. A read may not cross a 512-byte sector boundary.
Completion must observe DONE and BUSY clear, fence, capture error and verify
unchanged media before copying exactly count bytes to private user memory.
DONE is acknowledged before the pin/page is released and the IRQ is rearmed.

Invalid ranges/messages return `-EINVAL`; busy/reservation errors return
`-EBUSY`; stale wire generation returns `-EPIPE`. Device/timeout failure returns
`-EIO`; changed/absent media returns `-EPIPE`. Errors return zero count/data.
A malformed or stale client request does not terminate the service. An actual
media/DMA failure is terminal: the service attempts the error reply then exits,
revoking the endpoint and pending calls. A new floppy is never adopted under
the old generation. Device cancellation permanently forbids new submissions.

## Immutable file protocol

The Files server exposes one fixed file, `/font`, with file ID 1. Its content
is the raw bitmap byte extent, without the LAF index/header. The pre-issued
file endpoint is the capability; the file ID selects within that endpoint and
is not an independently transferable handle. There is no path parser, open
file table, mutable metadata, mount operation or write protocol.

| Message | Words after header | Bytes |
| --- | --- | --- |
| Read, type 1, payload 16 | u32 generation=1, u32 file ID=1, u32 byte offset, u32 count=1..16 | 20 |
| Stat, type 3, payload 12 | u32 generation=1, u32 file ID=1, zero | 16 |
| Result, type 2, payload 28 | i32 status, u32 generation=1, u32 count, 16 data bytes | 32 |

Stat returns file length with zero data. Read returns up to count bytes,
shortened at EOF or the next sector boundary. Reading exactly EOF succeeds
with zero bytes; an offset beyond EOF returns `-EINVAL`. Unknown IDs return
`-ENOENT`; invalid sizes/headers/counts return `-EINVAL`; stale generation
returns `-EPIPE`. All failures have zero count/data. Invalid client requests
are handled without forwarding a disk operation or terminating the service.

Every valid operation, including stat and EOF, first checks the disk's live
extent. The file server verifies response size/header/generation/status and
exact read count, stages successful bytes and publishes only the validated
count. A failed/dead disk or malformed disk reply is terminal for Files: it
attempts to forward the error (`-EPROTO` for inconsistent replies), then exits.
Death of Files cancels its own outstanding disk call and wakes its file clients;
Disk and Input continue independently. `fileRead` and `fileSize` are the public
bounded client helpers. One accept/reply loop serializes each service's calls.

## Exit, fault and generations

`taskFinish` handles both exit and fatal user traps. `taskAbortBlocked` handles
kernel termination of a suspended service. They cancel the task's own IPC wait,
destroy every endpoint it manages even if its management handle was closed,
close all its handles, mask/retire IRQ authority and revoke device operations.
Endpoint cancellation wakes both queued and accepted callers exactly once
with `-EPIPE` and clears reply rights. A remaining task continues scheduling.
Emergency supervisor UART/panic output requires none of these services.

A destroyed endpoint remains pinned while stale peer references exist. Calls
through those handles return `-EPIPE`; closing/reusing a handle slot advances
its generation and the old token becomes `-EBADF`. Object and IRQ generations
also advance on allocation, and exhausted generations retire rather than wrap.
This static profile retains terminal task snapshots and does not automatically
restart services. Bootstrap grants remain sealed; clients and dead services
cannot acquire replacement authority by guessing an identity. Generation 1
applies to this profile's single immutable resource incarnation. The separate
[recovery profile](SERVICE_RECOVERY.md) constructs fresh task/endpoint/IRQ/startup
identities under scoped user supervision, publishes them transactionally and
requires explicit client resolution; it never rebinds old handles.

## DMA service destruction

Logical death and IPC cancellation happen immediately. The disk broker then
disables the dead owner's command submission and abandons its current
operation. IRQ masking, a timeout or owner death does not stop DMA. WRM's disk
has no per-device stop command: only a whole-machine reset can abort a transfer.
Therefore cleanup waits for physical quiescence instead of inventing an abort.

`fontReap` retains the allocator pin while BUSY remains set. Once BUSY clears,
it fences, acknowledges DONE, fences again and releases the pin/buffer. The
normal task reaper calls `serviceDevicesQuiescent` before destroying the DMA
owner's directory, user pages or kernel stack. Until quiescence these resources
stay quarantined in a non-runnable Dead task; other tasks/timer IRQs continue.
A permanently stuck device keeps the buffer and owner resources quarantined
until reset. Cleanup never publishes unfinished bytes or starts another command.

# Approved device resources and operations (A7)

Date: 2026-10-05; resource table and storage root format revised 2026-10-08
([G2](GAP_02_KERNEL_POLICY.md)); write and flush implemented 2026-10-08
([G7](GAP_07_APPLICATION_LAYER.md)). This contract describes the implemented
broker: reads, and writes and flush under the
[write/flush contract](#multi-sector-and-writeflush-contract) below. It must be
extended before adding audio DMA or another physical engine. The Ethernet card is
brokered the same way ([NETWORK.md](NETWORK.md)). It does not grant an unrestricted driver MMIO page.

## Trust boundary

WRM device DMA uses physical addresses and bypasses the MMU. The kernel owns
physical register selection, bounce allocation, allocator pins, command stores,
fences, physical completion checks, user-copy validation and IRQ masks. Timer,
PIC and emergency UART remain kernel responsibilities. The emergency UART has
no dependency on rendering, font loading, IPC or a user output service.

The trusted resource manager holds a nontransferable `deviceFactory` mask and
CONFIGURE authority for specific children. Bootstrap issues an approved storage
root from `fonts/storage-extent.bin`, a producer-neutral 16-byte record written
by [storage_root.py](../tools/storage_root.py):

| Offset | Field | Value |
| ---: | --- | --- |
| 0 | `magic` | `WSR1` (`0x31525357`) |
| 4 | `version` | 1 |
| 8 | `bytes` | approved length, `1..0x7fffffff` |
| 12 | `flags` | bit 0: the [write contract](#multi-sector-and-writeflush-contract) may grant write authority (`--writable`); the root must then cover whole sectors. All other bits are reserved, zero |

The kernel reads only this framing (`approvedStorageBytes` in
[resources.m](../src/drivers/resources.m)). A wrong magic, version, length, an
undefined flag bit, or a writable root that is not whole sectors yields zero bytes
and `serviceDevicesInit` refuses to adopt it. `approvedStorageWritable` is true only for a valid root
that carries bit 0.
The bytes the root covers have no kernel-visible format. `pack_unifont.py
--extent` is the current producer and calls the same writer; a filesystem image
or test fixture can issue a root without touching the broker or the font tools.
The root starts after the boot image; its rounded sector coverage must fit the
selected medium. A kernel-only `deviceExtentInit` constructor accepts an
independent first sector and length.

## Device resource table

Which resource goes to which role is data, not code. The rows live in
[device_table.m](../src/drivers/device_table.m); the functions beside them are
the mechanism. Each row is `kind, role, virtual, physical, bytes, line`.

| Kind | Meaning | Permissions the kind implies |
| --- | --- | --- |
| `VRAM` | exclusive bounded VRAM alias | RW, user, never executable |
| `MMIO` | one read-safe register page | RO, user, never executable |
| `BLOB` | kernel-embedded read-only blob, named by index | RO, user, never executable |
| `IRQ` | interrupt line for a role | none (no mapping) |
| `DISK` | disk register page the broker drives, with its IRQ line | none (never mapped) |

Permissions are not a column: a row cannot ask for a writable register page.
The MMU accepts a resource grant only if it equals a valid row in placement,
size and permissions; `irqIssue` accepts a line only if a valid row names it;
the Screen register policy (`registerRules`: offset, writable bits, idle flag,
frame check) and the VRAM bound for scanout come from the same table. Bootstrap
iterates a role's rows instead of naming resources, and `serviceDiskIrq` is a
table lookup.

The mechanism ignores a row that breaks a kernel invariant, so a mistaken or
tampered row grants nothing instead of widening authority:

- an `MMIO` row must be one page that the hardware classes as read-safe
  (`DEVICE_READ_SAFE_PAGES`, currently the video page). Pages with destructive
  reads, DMA authority or global effect (PIC, keyboard, UART, timer, power,
  disks, RNG and the others in the inventory below) cannot be mapped;
- a `VRAM` row must lie wholly inside the VRAM window, below the register window;
- a `DISK` row cannot name a kernel-owned page or a read-safe page;
- no row can name the timer line or a line above 31, and the kernel keeps the
  timer, the PIC and the emergency UART regardless of the table;
- a `BLOB` row must resolve to a range inside kernel rodata.

Adding a resource of an already classified kind, moving one in the user address
space, changing an IRQ line or narrowing a register mask is a row edit. A new
read-safe register page or a new hardware class still needs a kernel change,
because that is a hardware property, not a policy. The user ABI constants
(`SCREEN_*_VA`) must match the rows: the Screen service links against them.
Runtime registration of rows is out of scope; the table is fixed at build.

An untrusted user driver interprets requests, paths, file IDs, font indices,
glyph chunks, display mode/palette choices and keyboard delivery policy. It
cannot choose a DMA address, scatter/gather list or hardware command flags.
Input's 32-event software queue, four-event replies and overflow delivery are
in `user/services/input.m`. The kernel returns raw bounded FIFO batches.

## Storage authority and ABI

`DeviceExtent` records the full task reference, resource generation, first
sector, byte length, revocation and medium invalidation. `DeviceOperation`
records immutable owner, monotonic instance, resource generation, copy offset,
length and logical cancellation. The physical reservation `deviceBounce` is
separate from the owner's page tables and remains owned by the broker.

| Syscall | Arguments | Result / authority |
| --- | --- | --- |
| `SYS_DEVICE_INFO` (70) | none | Approved byte length; exact Disk/Font owner only |
| `SYS_DEVICE_SUBMIT` (71) | relative byte offset, length, command, user source | Positive operation instance; command READ (1), WRITE (2) or FLUSH (3); the source address is for WRITE only |
| `SYS_DEVICE_FINISH` (72) | instance, user destination | Copied length or error; exact owner, instance and resource generation |
| `SYS_DEVICE_CANCEL` (73) | instance | Logical cancellation and grant revocation; no physical abort |
| `SYS_DEVICE_EXTENT` (74) | child reference, root-relative byte offset, length, flags | Manager selects an unpublished child's approved subrange; flags bit 0 asks for write authority |
| `SYS_DEVICE_FLAGS` (77) | none | Extent flags for the exact owner: 1 writable (extent, root and drive all allow it), 2 dirty (a WRITE not yet flushed), 4 dirty at regrant |

A submitted logical range is nonempty, wholly within its extent and touches at
most eight sectors (one bounce page). The physical command moves the touched
sectors through an aligned, exclusively allocated, pinned, zero-filled kernel
page. For READ, sector rounding may read trailing medium bytes; they cannot be
published outside the approved logical extent. Offset subtraction precedes
addition, preventing wrapping or crossing. Zero, oversized, wrapping, foreign,
stale and malformed requests issue no hardware command. IDENTIFY, LIST and all
extra flags are rejected. No user request contains a physical address; the one
user address, a WRITE source, is copied by the kernel before the command.

The manager may select a sector-aligned offset and a nonzero byte length within
the immutable approved root. It needs both its device factory and its child's
CONFIGURE authority. Configured/published children and active operations cannot
be resized. `recoveryDiskOffset` and `recoveryDiskBytes` in the user recovery
policy are configuration choices; zero length retains the full approved root.
The child cannot enlarge its resource. Runtime regrant is exclusive, requires
the previous owner to be dead, preflights quiescence before issuing a fresh IRQ
and never clears CHANGED. Medium replacement permanently invalidates this root.

Instances and resource generations stop at `0x7fffffff`; they never wrap.
Finish consumes the reservation once, including device error or invalid output
buffer. A stale instance cannot finish or cancel the next operation. A complete
output span is validated before publishing bytes; resource/MMIO mappings are
not ordinary IPC/copy buffers.

Legacy Font syscalls 27–30 and Disk syscalls 32–35 are reserved and return
ENOSYS. User Font/Disk helpers use the generic calls. This is an ABI migration:
kernel and embedded user images must be rebuilt together.

## Cancellation, timeout and reclamation

Cancellation is logical revocation plus eventual physical completion or
quiescence. The owner/instance record is preserved. BUSY keeps the allocator
reference and physical reservation, even after owner death, logical timeout,
IRQ masking or repeated reap. WRM supplies no per-device abort operation.

User drivers bound IRQ waits and cancel after timeout. An early finish also
cancels. Task death uses the same revocation path. A late completion is discarded
and releases the allocator reference/reservation once, only after BUSY clears
and trusted fences execute. Idle also reaps on its own validated stack, so a
late completion does not require another ready task or context switch. No DMA publication targets a dead owner. A dead
owner's task resources remain quarantined until its reservation is quiescent.
A permanently BUSY engine remains quarantined, rejects regrant and cannot stop
unrelated scheduling/IPC; it is not promised successful recovery without reset.

### Device reset (G3: specified as unavailable)

Checked against the [machine specification](../../docs/SPECIFICATION.md): WRM
defines **no per-device reset or abort**. A disk transfer "can't be stopped
except by a reset", and the only reset is the power controller's `RESET`
register, which resets the CPU and every device at once (`RESET_CAUSE` records
it). That page is kernel-only and is not granted to any user task
(see the inventory below). Consequently:

- LA/IX defines no device-reset operation, syscall or broker command, and no
  recovery path may claim to reset a stuck disk. A permanently BUSY disk stays
  quarantined as above; the only remedy is a machine reset by the operator or a
  future kernel-owned policy, which would also end every running service.
- Screen needs none. It issues no DMA, so its handover has nothing to quiesce
  beyond the reclamation of the dead owner's directory
  ([supervised display](SERVICE_RECOVERY.md#supervised-display-screen-and-bitmap-storage)).
- A per-device reset would first need a WRM hardware definition (a reset or
  abort register per device with documented effect on an in-flight transfer and
  on `BUSY`/`DONE`). Only then could a broker operation be specified, and it
  would still have to keep the allocator pin until `BUSY` is observed clear.

## IRQ and raw keyboard contract

IRQ grants are exclusive per physical line and carry a generation plus full
owner reference. A shared physical line is owned by one driver, which services
all device causes. Video DONE and VBLANK are two causes on line 5; disk DONE
and CHANGED share each disk line. The generic IRQ layer does not interpret a
font or display operation. The user Screen driver retries acknowledgement/rearm
at most four times when a VBLANK races servicing; a held level remains masked.

Notification masks the line and coalesces once per arm. A selected wait cannot
be completed by another owned line. Timeout wakes that selected waiter, retains
masking and does not imply device quiescence. Completion checks that every
shared cause/held level has cleared before enabling the line; foreign and stale
tokens cannot modify state. Asserted levels return EBUSY without interrupt
storms; rearm races retain the level for the next notification.

`SYS_INPUT_READ` (31) takes destination and capacity (1–32). It returns an
8-byte count/overflow prefix plus `capacity` four-byte raw HID words, zeroing
unused words. It validates the entire destination and current IRQ token before
reading destructive registers. It pops at most the requested capacity and
poll-rearms only a cleared line. Flush on issuance/release separates ownership
generations; it is not a user buffering or key interpretation policy.

## Display contract

`SYS_SCREEN_CONTROL` (26) takes a byte register offset and value. It is a checked
register write, with no built-in screen initialization sequence or palette.
Status acknowledges only DONE/VBLANK bits (mask 10). Other writes require a
quiescent drawing engine. CONTROL allows scanout and VBLANK IRQ only (mask 5).
MODE accepts documented resolution/depth bits, and MODE/START/scanout enable
must keep the entire visible frame within the granted VRAM extent. Palette
index is below 256, palette data is 24-bit RGB. These rules are rows of
`registerRules` in the device table: the writable bits and the idle requirement
of each register. The frame check, which decodes MODE and keeps the frame
inside the VRAM row's size, stays code because it interprets video hardware. Cursor control currently permits
only zero, pending a bounded cursor resource contract. All other writes fail.
`user/screen/video.m` chooses 640x480, 8bpp, black/white palette and CPU rendering.

The display owner is recorded by the kernel, not claimed by a task. The fixed
boot-time display is never regranted. A supervised display is issued by
`grantTaskDevices(child, SCREEN)` to an unpublished child once no live owner
exists, every Screen-role range is free in the resource ledger and the engine is
not BUSY; owner death stops scanout and clears video status
([contract](SERVICE_RECOVERY.md#supervised-display-screen-and-bitmap-storage)).

The video MMIO alias is RO/NX: its reads have no destructive FIFO semantics.
The writable mapping is only the exclusive bounded VRAM alias. No mixed MMIO
page containing a DMA command is mapped writable to a user task.

## Register and command authority inventory

The following inventories every register defined by WRM device headers, including
unused engines. All listed offsets are relative to the corresponding device
page. Each page also has a read-only identification word at `0xffc`. No new
mapping is implied by a register's read-only classification. Read-to-clear,
FIFO pop, RNG advancement and time latching are explicitly destructive reads.
The source of truth is `include/motherboard.h`, `include/devices/*.h` and the
matching `source/devices/*.c` implementations. Those sources are inspected,
never rebuilt, by this work.

| Device | Complete register list (hex offsets) | Access / operation decision |
| --- | --- | --- |
| pic | `PIC_REG_PENDING` (0x00), `PIC_REG_ENABLE` (0x04), `PIC_REG_ACTIVE` (0x08), `PIC_REG_CLAIM` (0x0C) | PENDING/ACTIVE/CLAIM are status reads. ENABLE globally masks IRQs: kernel only; no user mapping. |
| keyboard | `KEYBOARD_REG_STATUS` (0x00), `KEYBOARD_REG_DATA` (0x04), `KEYBOARD_REG_CONTROL` (0x08) | STATUS clears overflow; DATA pops FIFO. CONTROL flushes events. Exclusive raw-batch broker; no user MMIO mapping. |
| uart | `UART_REG_DATA` (0x00), `UART_REG_STATUS` (0x04), `UART_REG_CONTROL` (0x08) | DATA RX pops; STATUS clears overflow; CONTROL flushes RX. Only checked TX byte operation is granted; emergency UART is kernel-direct. |
| pit | `PIT_REG_COUNT_LO` (0x00), `PIT_REG_COUNT_HI` (0x04), `PIT_REG_FREQUENCY` (0x08), `PIT_REG_RELOAD` (0x0C), `PIT_REG_VALUE` (0x10), `PIT_REG_CONTROL` (0x14), `PIT_REG_STATUS` (0x18) | COUNT/FREQUENCY/VALUE are reads. RELOAD/CONTROL alter global scheduling and STATUS acknowledges timer IRQ: kernel only. |
| power | `POWER_REG_OFF` (0x00), `POWER_REG_RESET` (0x04), `POWER_REG_STATUS` (0x08), `POWER_REG_RESET_CAUSE` (0x0C) | STATUS/reset cause are reads, STATUS W1C. OFF/RESET globally stop/reset the machine: kernel only. |
| disk | `DISK_REG_STATUS` (0x00), `DISK_REG_SECTORS` (0x04), `DISK_REG_SECTOR` (0x08), `DISK_REG_COUNT` (0x0C), `DISK_REG_ADDRESS` (0x10), `DISK_REG_COMMAND` (0x14), `DISK_REG_ERROR` (0x18), `DISK_REG_LIST` (0x1C) | STATUS/SECTORS/ERROR are status reads; STATUS W1C. SECTOR/COUNT/ADDRESS/LIST/COMMAND control physical DMA: trusted broker only. READ approved; WRITE and FLUSH approved only under the write contract; IDENTIFY/LIST denied. |
| videocard | `VIDEO_REG_STATUS` (0x00), `VIDEO_REG_CONTROL` (0x04), `VIDEO_REG_MODE` (0x08), `VIDEO_REG_WIDTH` (0x0C), `VIDEO_REG_HEIGHT` (0x10), `VIDEO_REG_BPP` (0x14), `VIDEO_REG_PITCH` (0x18), `VIDEO_REG_VRAM_SIZE` (0x1C), `VIDEO_REG_START` (0x20), `VIDEO_REG_FRAME` (0x24), `VIDEO_REG_PALETTE_INDEX` (0x28), `VIDEO_REG_PALETTE_DATA` (0x2C), `VIDEO_REG_COMMAND` (0x40), `VIDEO_REG_ERROR` (0x44), `VIDEO_REG_DST_BASE` (0x48), `VIDEO_REG_DST_PITCH` (0x4C), `VIDEO_REG_DST_XY` (0x50), `VIDEO_REG_SRC_BASE` (0x54), `VIDEO_REG_SRC_PITCH` (0x58), `VIDEO_REG_SRC_XY` (0x5C), `VIDEO_REG_SIZE` (0x60), `VIDEO_REG_FG` (0x64), `VIDEO_REG_BG` (0x68), `VIDEO_REG_ADDRESS` (0x6C), `VIDEO_REG_COUNT` (0x70), `VIDEO_REG_CURSOR_CONTROL` (0x80), `VIDEO_REG_CURSOR_BASE` (0x84), `VIDEO_REG_CURSOR_XY` (0x88), `VIDEO_REG_CURSOR_HOT` (0x8C) | WIDTH/HEIGHT/BPP/PITCH/VRAM_SIZE/FRAME/ERROR and register readbacks are safe under exclusive ownership. Checked STATUS/CONTROL/MODE/START/PALETTE writes only; cursor disable only. Drawing/address/count/surface/cursor writes otherwise denied. FILL/COPY/EXPAND/LOAD/STORE, MEMORY and TRANSPARENT command flags have no user operation. |
| beeper | `BEEPER_REG_CONTROL` (0x00), `BEEPER_REG_FREQUENCY` (0x04), `BEEPER_REG_DURATION` (0x08) | Readbacks have no DMA. CONTROL/FREQUENCY/DURATION alter global sound: ungranted. |
| mouse | `MOUSE_REG_STATUS` (0x00), `MOUSE_REG_DATA` (0x04), `MOUSE_REG_CONTROL` (0x08), `MOUSE_REG_POSITION` (0x0C) | STATUS clears overflow; DATA pops FIFO; POSITION is last-event status. CONTROL flush/enable/absolute changes input capture: ungranted. |
| ethcard | `ETH_REG_STATUS` (0x00), `ETH_REG_CONTROL` (0x04), `ETH_REG_PENDING` (0x08), `ETH_REG_MAC_LO` (0x0C), `ETH_REG_MAC_HI` (0x10), `ETH_REG_RX_RING` (0x14), `ETH_REG_RX_SIZE` (0x18), `ETH_REG_RX_NEXT` (0x1C), `ETH_REG_TX_RING` (0x20), `ETH_REG_TX_SIZE` (0x24), `ETH_REG_TX_NEXT` (0x28), `ETH_REG_TX_KICK` (0x2C) | STATUS/MAC/RX_NEXT/TX_NEXT are status reads. CONTROL enables physical ring DMA; RING/SIZE/KICK and descriptor ownership give physical memory authority. PENDING is W1C. **Brokered since G7 ([NETWORK.md](NETWORK.md)):** the kernel alone programs the page, owns the rings and the buffers, and exposes only whole-frame send, receive and info calls to the one `DEVICE_NET` owner; no register is mapped. |
| audiocard | `AUDIO_REG_STATUS` (0x00), `AUDIO_REG_FAULT` (0x04), `AUDIO_REG_MASTER` (0x08), `AUDIO_REG_VOICES` (0x0C), `AUDIO_REG_RATE` (0x10), `AUDIO_REG_VOICE0` (0x100), `AUDIO_VOICE_CONTROL` (0x00), `AUDIO_VOICE_ADDRESS` (0x04), `AUDIO_VOICE_LENGTH` (0x08), `AUDIO_VOICE_LOOP` (0x0C), `AUDIO_VOICE_POSITION` (0x10), `AUDIO_VOICE_RATE` (0x14), `AUDIO_VOICE_VOLUME` (0x18) | VOICES/RATE and readbacks are status. STATUS/FAULT acknowledge shared causes; MASTER alters sound. Voice CONTROL starts physical DMA using ADDRESS/LENGTH/LOOP/POSITION/RATE/VOLUME: ungranted. |
| rtc | `RTC_REG_SECONDS_LO` (0x00), `RTC_REG_SECONDS_HI` (0x04), `RTC_REG_NANOSECONDS` (0x08), `RTC_REG_UTC_OFFSET` (0x0C), `RTC_REG_ALARM_LO` (0x10), `RTC_REG_ALARM_HI` (0x14), `RTC_REG_CONTROL` (0x18), `RTC_REG_STATUS` (0x1C) | SECONDS_LO latches time; other time fields are latched reads. ALARM/CONTROL and STATUS W1C affect IRQ policy: ungranted. |
| rng | `RNG_REG_DATA` (0x00), `RNG_REG_STATUS` (0x04) | DATA advances generator; STATUS is read-only. Kernel RNG only; no user mapping. |
| share | `SHARE_REG_STATUS` (0x00), `SHARE_REG_COMMAND` (0x04), `SHARE_REG_ERROR` (0x08), `SHARE_REG_HANDLE` (0x0C), `SHARE_REG_PATH` (0x10), `SHARE_REG_PATH2` (0x14), `SHARE_REG_ADDRESS` (0x18), `SHARE_REG_COUNT` (0x1C), `SHARE_REG_POSITION_LO` (0x20), `SHARE_REG_POSITION_HI` (0x24), `SHARE_REG_FLAGS` (0x28), `SHARE_REG_RESULT` (0x2C), `SHARE_REG_HANDLES` (0x30) | STATUS/ERROR/RESULT/HANDLES are status reads. COMMAND uses physical PATH/PATH2/ADDRESS and can change host files. HANDLE/COUNT/POSITION/FLAGS are command parameters. OPEN/CLOSE/READ/WRITE/STAT/READDIR/MKDIR/REMOVE/RENAME/TRUNCATE/SYNC: all ungranted. |
| watchdog | `WATCHDOG_REG_CONTROL` (0x00), `WATCHDOG_REG_TIMEOUT` (0x04), `WATCHDOG_REG_GRACE` (0x08), `WATCHDOG_REG_KICK` (0x0C), `WATCHDOG_REG_VALUE` (0x10), `WATCHDOG_REG_STATUS` (0x14) | VALUE/STATUS are reads; CONTROL/TIMEOUT/GRACE/KICK and STATUS W1C affect global IRQ/reset, with irreversible locking until reset: ungranted. |

Disk0, Disk1 and Floppy share the disk inventory. Audio voice register offsets
are relative to `VOICE0 + voice * 0x20`; eight voices are present. No unspecified
register or reserved command bit is an approved operation. RAM, ROM and VRAM
are memory resources, not authority to issue physical DMA.

## Multi-sector and write/flush contract

Status: **implemented 2026-10-08** ([G7](GAP_07_APPLICATION_LAYER.md)); source
accepted ([test_block_write](../tests/test_block_write.py), 13 cases, and the
updated [test_device_boundary](../tests/test_device_boundary.py) and
[test_device_safety](../tests/test_device_safety.py)); CPU evidence is the `fs`
profile ([FILESYSTEM.md](FILESYSTEM.md)), written but not yet run on a CPU. The
broker rejects IDENTIFY, LIST and every extra flag. Differences from the original
design: the flags are reported by their own syscall (`SYS_DEVICE_FLAGS`, 77)
instead of `SYS_DEVICE_INFO`, so the length stays a plain positive result, and
the boot policy `diskDevicesInitWritable` gives a trusted filesystem owner the
whole writable root without a manager step. It follows the WRM disk rules in `docs/SPECIFICATION.md`: `WRITE` and
`READ` move `COUNT` sectors at 4,000,000 bytes per second, a transfer cannot be
stopped except by reset, a write never stores a partial sector, and `FLUSH`
finishes in the tick of the store.

**Operation.** One operation per engine and one pinned bounce owner per
operation, as now. The bounce stays one allocator page, so an operation moves
at most **8 sectors (4096 bytes)**, about one millisecond of device time.
`COUNT` is the number of sectors the range touches; a range touching more than
eight is rejected. A larger transfer is several operations issued by the
driver; the kernel keeps no queue and no scatter/gather list (`LIST` stays
denied).

| Command | Range rule | Direction and timing |
| --- | --- | --- |
| `READ` (1) | any nonempty byte range inside the extent that touches at most 8 sectors | device to bounce, then published by `FINISH` as now |
| `WRITE` (2) | offset and length both multiples of 512, at most 4096 bytes, inside the extent | user buffer to bounce at `SUBMIT`, then bounce to device |
| `FLUSH` (3) | offset and length must be zero | no data; completes with the store |

The kernel does no read-modify-write. A sub-sector update is the user driver's
policy: READ the sector, change it, WRITE it back.

**Submission ABI.** `SYS_DEVICE_SUBMIT` gains a fourth argument, the user
source address, which must be zero for READ and FLUSH. For WRITE the kernel
validates the whole span and copies it into the zero-filled bounce **before**
the command, so no DMA ever reads user memory later and an invalid buffer
issues no command. The ABI change requires rebuilding kernel and embedded user
images together, like the earlier migration.

**Write authority.** Three conditions must all hold, and any one missing issues
no command:

1. the storage root has `flags` bit 0 (writable) set;
2. the manager selected a writable extent: `SYS_DEVICE_EXTENT` gains a fourth
   argument, a flags word whose bit 0 requests write, accepted only for an
   unpublished child and only if the root allows it;
3. the drive does not report `STATUS.READONLY`.

A read-only root, extent or drive makes WRITE and FLUSH fail with a new
`-EROFS` (30) and no hardware access. The default everywhere stays read-only. A
regrant after owner death returns the extent to read-only until the manager
selects it again, so a replacement never inherits write authority implicitly.
`SYS_DEVICE_INFO` reports the extent flags with the length.

**Durability.** `WRITE` completion means the device accepted the sectors, not
that the host medium holds them. Each extent keeps a `dirty` bit set when a
WRITE is issued and cleared when a FLUSH finishes without error. FLUSH needs
write authority and, like every command, is serialized behind the single
outstanding operation. A device error leaves `dirty` set. The kernel never
flushes on a driver's behalf; the driver (or the filesystem above it) flushes
before it reports a write durable and before power-off.

**Cancellation, timeout and death.** Unchanged: cancellation is logical and
cannot abort the transfer. A cancelled or orphaned WRITE still lands, so
cancellation does not undo it. The pin and reservation are released only after
BUSY clears and the trusted fences run. An owner that dies with `dirty` set
leaves the extent in an indeterminate state: regrant is allowed, the extent
comes back read-only, and `SYS_DEVICE_INFO` reports a `dirty-at-regrant` flag so
the manager's policy can scrub, verify or refuse. A medium change still
invalidates the root for the boot.

**Errors.** `ERROR` 3 (range) and 4 (address) cannot occur for a validated
range and bounce, so they are internal faults. 5 maps to `-EROFS`, 6 to
`-EIO`. All results are consumed exactly once by `FINISH`.

**Evidence.** The source cases below pass; the CPU cases run only through the
`fs` profile. Source cases: no command without root,
extent and drive write authority; alignment and length limits; a faulting user
buffer issues no command and leaves the page unpinned; instance exhaustion and
stale instances; `dirty` set by WRITE, cleared only by a successful FLUSH;
regrant returns read-only and reports `dirty-at-regrant`; owner death, timeout
and late completion with a WRITE in flight keep the pin until BUSY clears (the
existing DMA safety campaign, unchanged). CPU cases on a scratch image: write,
flush and read back; read-only image and read-only root return `-EROFS`;
cancellation mid-write leaves the page pinned and the next operation correct.
The `fs` profile runs a write, a flush and a read back through the real DMA path,
and snapshots the medium at every WRITE and FLUSH command. A read-only image, a
read-only root and cancellation mid-write have source tests only.

## Production linkage

Ordinary images exclude `drivers/videocard`, `console/console`,
`console/font/font` and `console/font/glyph_cache`. Only explicitly selected
`tests/programs/console/` supervisor regression mains link those modules.
Every image links `drivers/device_table`, the resource rows and their mechanism. The
read-only catalog resource remains for the screen service and MMU resource
validation; it is not a kernel font parser or renderer. Direct debug UART and
panic remain independently linked.

See [A7 acceptance](../tests/DEVICE_BOUNDARY_ACCEPTANCE.md) for checked-source,
CPU/device evidence, reproducibility, provenance and limitations.

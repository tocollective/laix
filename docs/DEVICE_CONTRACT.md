# Approved device resources and operations (A7)

Date: 2026-10-05. This contract describes the implemented read-only broker.
It must be extended before adding disk writes, flush, networking, audio DMA
or another physical engine. It does not grant an unrestricted driver MMIO page.

## Trust boundary

WRM device DMA uses physical addresses and bypasses the MMU. The kernel owns
physical register selection, bounce allocation, allocator pins, command stores,
fences, physical completion checks, user-copy validation and IRQ masks. Timer,
PIC and emergency UART remain kernel responsibilities. The emergency UART has
no dependency on rendering, font loading, IPC or a user output service.

The trusted resource manager holds a nontransferable `deviceFactory` mask and
CONFIGURE authority for specific children. Bootstrap issues an approved storage
root from `fonts/storage-extent.bin`, a four-byte little-endian byte count.
`pack_unifont.py --extent` is the current resource producer. The broker does not
interpret the producer's font format. The root starts after the boot image;
its rounded sector coverage must fit the selected medium. Another producer can
issue a different approved resource without changing the broker. A kernel-only
`deviceExtentInit` constructor accepts an independent first sector and length.

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
| `SYS_DEVICE_SUBMIT` (71) | relative byte offset, length, command | Positive operation instance; command must equal READ (1) |
| `SYS_DEVICE_FINISH` (72) | instance, user destination | Copied length or error; exact owner, instance and resource generation |
| `SYS_DEVICE_CANCEL` (73) | instance | Logical cancellation and grant revocation; no physical abort |
| `SYS_DEVICE_EXTENT` (74) | child reference, root-relative byte offset, length | Manager selects an unpublished child's approved subrange |

A submitted logical range is nonempty, at most 512 bytes, wholly within its
extent and one sector. The physical command always reads exactly one sector
into an aligned, exclusively allocated, pinned kernel page. Sector rounding
may read trailing medium bytes; they cannot be published outside the approved
logical extent. Offset subtraction precedes addition, preventing wrapping or
crossing. Zero, oversized, wrapping, foreign, stale and malformed requests
issue no hardware command. WRITE, FLUSH, IDENTIFY, LIST and all extra flags are
rejected. No user request contains a physical address.

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
index is below 256, palette data is 24-bit RGB. Cursor control currently permits
only zero, pending a bounded cursor resource contract. All other writes fail.
`user/screen/video.m` chooses 640x480, 8bpp, black/white palette and CPU rendering.

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
| disk | `DISK_REG_STATUS` (0x00), `DISK_REG_SECTORS` (0x04), `DISK_REG_SECTOR` (0x08), `DISK_REG_COUNT` (0x0C), `DISK_REG_ADDRESS` (0x10), `DISK_REG_COMMAND` (0x14), `DISK_REG_ERROR` (0x18), `DISK_REG_LIST` (0x1C) | STATUS/SECTORS/ERROR are status reads; STATUS W1C. SECTOR/COUNT/ADDRESS/LIST/COMMAND control physical DMA: trusted broker only. READ approved; WRITE/FLUSH/IDENTIFY/LIST denied. |
| videocard | `VIDEO_REG_STATUS` (0x00), `VIDEO_REG_CONTROL` (0x04), `VIDEO_REG_MODE` (0x08), `VIDEO_REG_WIDTH` (0x0C), `VIDEO_REG_HEIGHT` (0x10), `VIDEO_REG_BPP` (0x14), `VIDEO_REG_PITCH` (0x18), `VIDEO_REG_VRAM_SIZE` (0x1C), `VIDEO_REG_START` (0x20), `VIDEO_REG_FRAME` (0x24), `VIDEO_REG_PALETTE_INDEX` (0x28), `VIDEO_REG_PALETTE_DATA` (0x2C), `VIDEO_REG_COMMAND` (0x40), `VIDEO_REG_ERROR` (0x44), `VIDEO_REG_DST_BASE` (0x48), `VIDEO_REG_DST_PITCH` (0x4C), `VIDEO_REG_DST_XY` (0x50), `VIDEO_REG_SRC_BASE` (0x54), `VIDEO_REG_SRC_PITCH` (0x58), `VIDEO_REG_SRC_XY` (0x5C), `VIDEO_REG_SIZE` (0x60), `VIDEO_REG_FG` (0x64), `VIDEO_REG_BG` (0x68), `VIDEO_REG_ADDRESS` (0x6C), `VIDEO_REG_COUNT` (0x70), `VIDEO_REG_CURSOR_CONTROL` (0x80), `VIDEO_REG_CURSOR_BASE` (0x84), `VIDEO_REG_CURSOR_XY` (0x88), `VIDEO_REG_CURSOR_HOT` (0x8C) | WIDTH/HEIGHT/BPP/PITCH/VRAM_SIZE/FRAME/ERROR and register readbacks are safe under exclusive ownership. Checked STATUS/CONTROL/MODE/START/PALETTE writes only; cursor disable only. Drawing/address/count/surface/cursor writes otherwise denied. FILL/COPY/EXPAND/LOAD/STORE, MEMORY and TRANSPARENT command flags have no user operation. |
| beeper | `BEEPER_REG_CONTROL` (0x00), `BEEPER_REG_FREQUENCY` (0x04), `BEEPER_REG_DURATION` (0x08) | Readbacks have no DMA. CONTROL/FREQUENCY/DURATION alter global sound: ungranted. |
| mouse | `MOUSE_REG_STATUS` (0x00), `MOUSE_REG_DATA` (0x04), `MOUSE_REG_CONTROL` (0x08), `MOUSE_REG_POSITION` (0x0C) | STATUS clears overflow; DATA pops FIFO; POSITION is last-event status. CONTROL flush/enable/absolute changes input capture: ungranted. |
| ethcard | `ETH_REG_STATUS` (0x00), `ETH_REG_CONTROL` (0x04), `ETH_REG_PENDING` (0x08), `ETH_REG_MAC_LO` (0x0C), `ETH_REG_MAC_HI` (0x10), `ETH_REG_RX_RING` (0x14), `ETH_REG_RX_SIZE` (0x18), `ETH_REG_RX_NEXT` (0x1C), `ETH_REG_TX_RING` (0x20), `ETH_REG_TX_SIZE` (0x24), `ETH_REG_TX_NEXT` (0x28), `ETH_REG_TX_KICK` (0x2C) | STATUS/MAC/RX_NEXT/TX_NEXT are status reads. CONTROL enables physical ring DMA; RING/SIZE/KICK and descriptor ownership give physical memory authority. PENDING is W1C: entire engine ungranted. |
| audiocard | `AUDIO_REG_STATUS` (0x00), `AUDIO_REG_FAULT` (0x04), `AUDIO_REG_MASTER` (0x08), `AUDIO_REG_VOICES` (0x0C), `AUDIO_REG_RATE` (0x10), `AUDIO_REG_VOICE0` (0x100), `AUDIO_VOICE_CONTROL` (0x00), `AUDIO_VOICE_ADDRESS` (0x04), `AUDIO_VOICE_LENGTH` (0x08), `AUDIO_VOICE_LOOP` (0x0C), `AUDIO_VOICE_POSITION` (0x10), `AUDIO_VOICE_RATE` (0x14), `AUDIO_VOICE_VOLUME` (0x18) | VOICES/RATE and readbacks are status. STATUS/FAULT acknowledge shared causes; MASTER alters sound. Voice CONTROL starts physical DMA using ADDRESS/LENGTH/LOOP/POSITION/RATE/VOLUME: ungranted. |
| rtc | `RTC_REG_SECONDS_LO` (0x00), `RTC_REG_SECONDS_HI` (0x04), `RTC_REG_NANOSECONDS` (0x08), `RTC_REG_UTC_OFFSET` (0x0C), `RTC_REG_ALARM_LO` (0x10), `RTC_REG_ALARM_HI` (0x14), `RTC_REG_CONTROL` (0x18), `RTC_REG_STATUS` (0x1C) | SECONDS_LO latches time; other time fields are latched reads. ALARM/CONTROL and STATUS W1C affect IRQ policy: ungranted. |
| rng | `RNG_REG_DATA` (0x00), `RNG_REG_STATUS` (0x04) | DATA advances generator; STATUS is read-only. Kernel RNG only; no user mapping. |
| share | `SHARE_REG_STATUS` (0x00), `SHARE_REG_COMMAND` (0x04), `SHARE_REG_ERROR` (0x08), `SHARE_REG_HANDLE` (0x0C), `SHARE_REG_PATH` (0x10), `SHARE_REG_PATH2` (0x14), `SHARE_REG_ADDRESS` (0x18), `SHARE_REG_COUNT` (0x1C), `SHARE_REG_POSITION_LO` (0x20), `SHARE_REG_POSITION_HI` (0x24), `SHARE_REG_FLAGS` (0x28), `SHARE_REG_RESULT` (0x2C), `SHARE_REG_HANDLES` (0x30) | STATUS/ERROR/RESULT/HANDLES are status reads. COMMAND uses physical PATH/PATH2/ADDRESS and can change host files. HANDLE/COUNT/POSITION/FLAGS are command parameters. OPEN/CLOSE/READ/WRITE/STAT/READDIR/MKDIR/REMOVE/RENAME/TRUNCATE/SYNC: all ungranted. |
| watchdog | `WATCHDOG_REG_CONTROL` (0x00), `WATCHDOG_REG_TIMEOUT` (0x04), `WATCHDOG_REG_GRACE` (0x08), `WATCHDOG_REG_KICK` (0x0C), `WATCHDOG_REG_VALUE` (0x10), `WATCHDOG_REG_STATUS` (0x14) | VALUE/STATUS are reads; CONTROL/TIMEOUT/GRACE/KICK and STATUS W1C affect global IRQ/reset, with irreversible locking until reset: ungranted. |

Disk0, Disk1 and Floppy share the disk inventory. Audio voice register offsets
are relative to `VOICE0 + voice * 0x20`; eight voices are present. No unspecified
register or reserved command bit is an approved operation. RAM, ROM and VRAM
are memory resources, not authority to issue physical DMA.

## Production linkage

Ordinary images exclude `drivers/videocard`, `console/console`,
`console/font/font` and `console/font/glyph_cache`. Only explicitly selected
`tests/programs/console/` supervisor regression mains link those modules. The
read-only catalog resource remains for the screen service and MMU resource
validation; it is not a kernel font parser or renderer. Direct debug UART and
panic remain independently linked.

See [A7 acceptance](../tests/DEVICE_BOUNDARY_ACCEPTANCE.md) for checked-source,
CPU/device evidence, reproducibility, provenance and limitations.

// Narrow trusted hardware broker. User services cannot select MMIO, a DMA
// address, scatter-gather descriptors, disk writes or video DMA commands.
import { DISK0_BASE, DISK1_BASE, FLOPPY_BASE, DISK_PRESENT, DISK_CHANGED,
    DISK_BUSY, DISK_DONE, DISK_READ, SECTOR_SIZE, SECTOR_MASK, GLYPH_BYTES,
    PAGE_SIZE, VIDEO_BASE, VIDEO_BUSY, VIDEO_MODE_640_480, VIDEO_8BPP,
    SCREEN_WIDTH, SCREEN_HEIGHT, SCREEN_BPP, SCREEN_PITCH, ERRNO_EPERM,
    ERRNO_EINVAL, ERRNO_EBUSY, ERRNO_EPIPE, ERRNO_EIO } from "../arch/wrm081632/defs.m"
import { PAGE_NONE, PAGE_KERNEL, allocPage, freePage, retainPage, releasePage,
    physicalPageOwned } from "../mm/memory.m"
import { copyToUser } from "../mm/mmu.m"
import { taskGet, Task, TASK_CREATED } from "../task/task.m"
import { fontData, fontDataEnd } from "../console/font/data.m"
import { objectAssertAtomic } from "../ipc/objects.m"
import { panic } from "../kernel/panic.m"

type DiskRegisters {
    status: UWord,
    sectors: UWord,
    sector: UWord,
    count: UWord,
    address: UWord,
    command: UWord,
    error: UWord,
    list: UWord,
}
let DMA_BUFFER_OWNER: UWord = 0xFFFFFFFE
let mut fontDisk: *volatile mut DiskRegisters
let mut fontStorageOwner: UWord
let mut screenOwner: UWord
let mut fontFirstSector: UWord
let mut fontGlyphCount: UWord
let mut fontDisabled: Bool
let mut fontBounce: UWord
let mut fontOperationOwner: UWord
let mut fontOperationBytes: UWord
let mut fontOperationOffset: UWord
let mut devicesInitialized: Bool

let serviceDiskIrq(disk: UWord): UWord {
    if disk == DISK0_BASE return 3
    if disk == DISK1_BASE return 4
    if disk == FLOPPY_BASE return 6
    return 32
}

let serviceDevicesInit(screen: UWord, storage: UWord, disk: UWord, imageBytes: UWord): Bool {
    objectAssertAtomic()
    let screenTask: *mut Task = taskGet(screen)
    let storageTask: *mut Task = taskGet(storage)
    let bytes: UWord = (&fontDataEnd as UWord) - (&fontData as UWord)
    let header: *UWord = &fontData as *UWord
    if devicesInitialized || screen == storage || storageTask == null ||
        (screen != 0 && (screenTask == null || screenTask.state != TASK_CREATED)) || storageTask.state != TASK_CREATED ||
        serviceDiskIrq(disk) == 32 || imageBytes == 0 || imageBytes & SECTOR_MASK != 0 ||
        bytes < 32 || header[0] != 0x3146414C || header[1] != 1 || header[2] == 0 ||
        header[2] > 0x7FFFFFFF / GLYPH_BYTES || header[2] > (bytes - 32) / 8 || bytes != 32 + header[2] * 8 return false
    let drive: *volatile mut DiskRegisters = disk as *volatile mut DiskRegisters
    let first: UWord = imageBytes / SECTOR_SIZE
    let sectors: UWord = (header[2] + 15) / 16
    if drive.status & DISK_PRESENT == 0 || drive.status & DISK_BUSY != 0 ||
        first > drive.sectors || sectors > drive.sectors - first return false
    // Adopt the boot medium once. Subsequent CHANGED permanently invalidates
    // this boot resource, even if replacement media has the same size.
    drive.status = DISK_CHANGED | DISK_DONE
    fence()
    if drive.status & (DISK_PRESENT | DISK_CHANGED) != DISK_PRESENT return false
    fontDisk = drive
    fontFirstSector = first
    fontGlyphCount = header[2]
    screenOwner = screen
    fontStorageOwner = storage
    devicesInitialized = true
    fontDisabled = false
    return true
}

// Rollback before publication clears policy, but can never discard a live DMA
// buffer. Normal service death permanently revokes its grant for this boot.
let serviceDevicesRollback(): Void {
    objectAssertAtomic()
    if fontBounce != PAGE_NONE return
    fontDisk = null
    fontStorageOwner = 0
    screenOwner = 0
    devicesInitialized = false
}

let fontMediumLive(): Bool {
    if fontDisabled || fontDisk == null return false
    if fontDisk.status & (DISK_PRESENT | DISK_CHANGED) != DISK_PRESENT {
        fontDisabled = true
        return false
    }
    return true
}

let fontValidate(owner: UWord): Word {
    objectAssertAtomic()
    if owner == 0 || owner != fontStorageOwner return -ERRNO_EPERM
    if !fontMediumLive() return -ERRNO_EPIPE
    return 0
}

let fontReleaseBuffer(): Void {
    if fontBounce == PAGE_NONE return
    // The physical allocator pin is independent of both service tasks' roots.
    if !releasePage(fontBounce, DMA_BUFFER_OWNER, PAGE_KERNEL) ||
        !freePage(fontBounce, DMA_BUFFER_OWNER, PAGE_KERNEL) {
        panic("could not release completed font DMA", null)
        return
    }
    fontBounce = PAGE_NONE
    fontOperationOwner = 0
}

let diskDevicesInit(owner: UWord, disk: UWord, imageBytes: UWord): Bool {
    return serviceDevicesInit(0, owner, disk, imageBytes)
}

let diskInfo(owner: UWord): Word {
    let status: Word = fontValidate(owner)
    if status != 0 return status
    return (fontGlyphCount * GLYPH_BYTES) as Word
}

let diskBegin(owner: UWord, offset: UWord, bytes: UWord): Word {
    let status: Word = fontValidate(owner)
    if status != 0 return status
    let extent: UWord = fontGlyphCount * GLYPH_BYTES
    if bytes == 0 || bytes > 16 || offset >= extent || bytes > extent - offset ||
        bytes > SECTOR_SIZE - offset % SECTOR_SIZE return -ERRNO_EINVAL
    return fontSubmit(owner, fontFirstSector + offset / SECTOR_SIZE, offset % SECTOR_SIZE, bytes)
}

let fontBegin(owner: UWord, glyph: UWord, chunk: UWord): Word {
    objectAssertAtomic()
    let live: Word = fontValidate(owner)
    if live != 0 return live
    if glyph >= fontGlyphCount || chunk > 1 return -ERRNO_EINVAL
    return fontSubmit(owner, fontFirstSector + glyph / 16, (glyph % 16) * GLYPH_BYTES + chunk * 16, 16)
}

let fontSubmit(owner: UWord, sector: UWord, offset: UWord, bytes: UWord): Word {
    if fontBounce != PAGE_NONE || fontDisk.status & DISK_BUSY != 0 return -ERRNO_EBUSY
    if sector >= fontDisk.sectors return -ERRNO_EPIPE
    fontBounce = allocPage(DMA_BUFFER_OWNER, PAGE_KERNEL)
    if fontBounce == PAGE_NONE return -ERRNO_EBUSY
    if !physicalPageOwned(fontBounce, DMA_BUFFER_OWNER, PAGE_KERNEL) ||
        fontBounce % PAGE_SIZE != 0 || SECTOR_SIZE > PAGE_SIZE ||
        !retainPage(fontBounce, DMA_BUFFER_OWNER, PAGE_KERNEL) {
        if !freePage(fontBounce, DMA_BUFFER_OWNER, PAGE_KERNEL) panic("invalid DMA reservation", null)
        fontBounce = PAGE_NONE
        return -ERRNO_EIO
    }
    let buffer: *mut UWord = fontBounce as *mut UWord
    for i: UWord in 0..(SECTOR_SIZE / 4) buffer[i] = 0
    fontOperationOwner = owner
    fontOperationOffset = offset
    fontOperationBytes = bytes
    // The entire transfer lies in the pinned page, never in a user VA. No
    // untrusted command/address field can reach the register stores below.
    fontDisk.sector = sector
    fontDisk.count = 1
    fontDisk.address = fontBounce
    fence()
    fontDisk.command = DISK_READ
    fence()
    return 0
}

let fontCancelOwner(owner: UWord): Void {
    objectAssertAtomic()
    if owner == 0 || owner != fontStorageOwner return
    fontDisabled = true
    if fontOperationOwner == owner fontOperationOwner = 0
    // A mask, timeout or death cannot abort WRM disk DMA. Keep the pin until
    // BUSY clears; only then can late completion/quiescence release the page.
    fontReap()
}

// Device shutdown forbids new submissions. WRM has no per-disk abort, so
// final task/resource destruction must wait for observed physical quiescence.
let serviceDevicesQuiescent(owner: UWord): Bool {
    objectAssertAtomic()
    if owner != fontStorageOwner return true
    fontReap()
    return fontBounce == PAGE_NONE
}

let fontReap(): Void {
    objectAssertAtomic()
    if fontBounce == PAGE_NONE || fontOperationOwner != 0 || fontDisk == null ||
        fontDisk.status & DISK_BUSY != 0 return
    fence()
    fontDisk.status = DISK_DONE
    fence()
    fontReleaseBuffer()
}

let fontFinish(owner: UWord, destination: UWord): Word {
    objectAssertAtomic()
    if owner == 0 || owner != fontStorageOwner return -ERRNO_EPERM
    if fontBounce == PAGE_NONE || fontOperationOwner != owner return -ERRNO_EINVAL
    let state: UWord = fontDisk.status
    if state & DISK_BUSY != 0 || state & DISK_DONE == 0 {
        fontCancelOwner(owner)
        return -ERRNO_EIO
    }
    fence()
    let mut result: Word = -ERRNO_EIO
    if !fontMediumLive() result = -ERRNO_EPIPE
    else if fontDisk.error == 0 {
        let task: *mut Task = taskGet(owner)
        result = copyToUser(task.directory, owner, destination,
            (fontBounce + fontOperationOffset) as *UByte, fontOperationBytes)
        if result == 0 result = fontOperationBytes as Word
    }
    fontDisk.status = DISK_DONE
    fence()
    fontReleaseBuffer()
    return result
}

// Only mode/scanout/palette and W1C status are exposed. Rendering uses CPU
// stores into granted VRAM; COMMAND, ADDRESS and COUNT have no user operation.
let screenControl(owner: UWord, operation: UWord): Word {
    objectAssertAtomic()
    if owner == 0 || owner != screenOwner return -ERRNO_EPERM
    let registers: *volatile mut UWord = VIDEO_BASE as *volatile mut UWord
    if operation > 3 return -ERRNO_EINVAL
    if operation == 2 {
        registers[0] = 10 // W1C DONE and VBLANK, both causes of video IRQ 5
        fence()
        return 0
    }
    if registers[0] & VIDEO_BUSY != 0 return -ERRNO_EBUSY
    if operation == 0 {
        registers[1] = 0
        registers[2] = VIDEO_MODE_640_480 | VIDEO_8BPP
        registers[8] = 0
        registers[10] = 0
        registers[11] = 0
        registers[11] = 0xFFFFFF
        registers[32] = 0 // disable the firmware cursor
        if registers[3] != SCREEN_WIDTH || registers[4] != SCREEN_HEIGHT ||
            registers[5] != SCREEN_BPP || registers[6] != SCREEN_PITCH return -ERRNO_EIO
    } else if operation == 1 registers[1] = 5 // scanout + VBLANK IRQ only
    else registers[1] = 0
    fence()
    return 0
}

let screenReleaseOwner(owner: UWord): Void {
    if owner == 0 || owner != screenOwner return
    let registers: *volatile mut UWord = VIDEO_BASE as *volatile mut UWord
    registers[1] = 0
    registers[0] = 10
    fence()
    screenOwner = 0
}

export { serviceDiskIrq, serviceDevicesInit, serviceDevicesRollback,
    diskDevicesInit, diskInfo, diskBegin, serviceDevicesQuiescent,
    fontValidate, fontBegin, fontFinish, fontCancelOwner, fontReap,
    screenControl, screenReleaseOwner }

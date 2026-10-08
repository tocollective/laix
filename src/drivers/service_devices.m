// Trusted physical-device mechanism. Descriptors contain approved byte ranges;
// no filesystem, bitmap format, user address or user command reaches DMA MMIO.
import { DISK_PRESENT, DISK_CHANGED,
    DISK_BUSY, DISK_DONE, DISK_READ, DISK_WRITE, DISK_FLUSH, DISK_STATUS_READONLY,
    DISK_BOUNCE_SECTORS, EXTENT_WRITE, EXTENT_DIRTY, EXTENT_DIRTY_AT_REGRANT,
    SECTOR_SIZE, SECTOR_MASK, PAGE_SIZE,
    VIDEO_BUSY, ERRNO_EPERM, ERRNO_EINVAL, ERRNO_ENFILE,
    ERRNO_EBUSY, ERRNO_EPIPE, ERRNO_EIO, ERRNO_EOVERFLOW, ERRNO_EROFS } from "../arch/wrm081632/defs.m"
import { PAGE_NONE, PAGE_KERNEL, allocPage, freePage, retainPage, releasePage,
    physicalPageOwned } from "../mm/memory.m"
import { copyToUser, copyFromUser, mmuInstallResource, mmuResourceFree } from "../mm/mmu.m"
import { taskGet, Task, TASK_CREATED, TASK_DEAD } from "../task/task.m"
import { approvedStorageBytes, approvedStorageWritable } from "resources.m"
import { DEVICE_ROLE_SCREEN, DEVICE_NONE, DEVICE_ROW_COUNT, REGISTER_RULE_COUNT, REGISTER_IDLE, CHECK_ENABLE,
    CHECK_MODE, CHECK_START, deviceDiskIrq, deviceRoleRegisters, deviceRoleVramBytes,
    deviceRegisterRule, deviceRuleMask, deviceRuleFlags, deviceRuleCheck,
    deviceRoleMapping, deviceRoleMappingCount, deviceRowVirtual, deviceRowPhysical,
    deviceRowBytes, deviceRowPermissions } from "device_table.m"
import { objectAssertAtomic } from "../ipc/objects.m"
import { panic } from "../kernel/panic.m"

type DiskRegisters {
    status: UWord, sectors: UWord, sector: UWord, count: UWord,
    address: UWord, command: UWord, error: UWord, list: UWord,
}

type DeviceExtent {
    owner: UWord,
    generation: UWord,
    firstSector: UWord,
    bytes: UWord,
    revoked: Bool,
    mediumInvalid: Bool,
    // Write contract (G7). The default everywhere is read-only; a regrant
    // returns the extent to read-only and records an owner that died dirty.
    writable: Bool,
    dirty: Bool,
    dirtyAtRegrant: Bool,
}

type DeviceOperation {
    owner: UWord, // immutable full task reference, including slot generation
    instance: UWord, // monotonic, never wraps or resets on regrant
    resourceGeneration: UWord,
    offset: UWord,
    bytes: UWord,
    command: UWord,
    cancelled: Bool,
}
let DISK_ERROR_READONLY: UWord = 5
let DMA_BUFFER_OWNER: UWord = 0xFFFFFFFE
let mut deviceDisk: *volatile mut DiskRegisters
let mut deviceExtent: DeviceExtent
let mut deviceOperation: DeviceOperation
let mut deviceBounce: UWord
let mut screenOwner: UWord
// A screen owner issued at boot is part of the sealed bootstrap graph: its disk
// pair is never regranted. A runtime screen owner is replaceable (see below).
let mut screenFixed: Bool
let mut approvedFirstSector: UWord
let mut approvedBytes: UWord
// The storage root's writable bit, read when the root is adopted. Write
// authority needs this, an extent the manager selected as writable, and a drive
// that does not report READONLY.
let mut approvedWritable: Bool
let mut devicesInitialized: Bool

// IRQ line of an approved disk page, from the device table; 32 if not a row.
let serviceDiskIrq(disk: UWord): UWord {
    return deviceDiskIrq(disk)
}

// Bootstrap consumes a build-issued resource manifest, never a font header.
// The kernel-only constructor also supports independent approved resources.
let deviceExtentInit(screen: UWord, storage: UWord, disk: UWord,
    first: UWord, bytes: UWord): Bool {
    objectAssertAtomic()
    let screenTask: *mut Task = taskGet(screen)
    let storageTask: *mut Task = taskGet(storage)
    if devicesInitialized || deviceExtent.generation == 0x7FFFFFFF || screen == storage || storageTask == null ||
        (screen != 0 && (screenTask == null || screenTask.state != TASK_CREATED)) ||
        storageTask.state != TASK_CREATED || serviceDiskIrq(disk) == DEVICE_NONE ||
        bytes == 0 || bytes > 0x7FFFFFFF return false
    let drive: *volatile mut DiskRegisters = disk as *volatile mut DiskRegisters
    let sectors: UWord = (bytes - 1) / SECTOR_SIZE + 1
    if drive.status & DISK_PRESENT == 0 || drive.status & DISK_BUSY != 0 ||
        first > drive.sectors || sectors > drive.sectors - first return false
    // Initial adoption is trusted boot policy. Runtime regrant never clears a
    // medium-change cause, even when replacement media has the same capacity.
    drive.status = DISK_CHANGED | DISK_DONE
    fence()
    if drive.status & (DISK_PRESENT | DISK_CHANGED) != DISK_PRESENT return false
    deviceDisk = drive
    approvedFirstSector = first
    approvedBytes = bytes
    deviceExtent.owner = storage
    deviceExtent.generation += 1
    deviceExtent.firstSector = first
    deviceExtent.bytes = bytes
    deviceExtent.revoked = false
    deviceExtent.mediumInvalid = false
    deviceExtent.writable = false
    deviceExtent.dirty = false
    deviceExtent.dirtyAtRegrant = false
    approvedWritable = false
    // A disk-only init (screen == 0) must not disturb a runtime display owner.
    if screen != 0 {
        screenOwner = screen
        screenFixed = true
    }
    devicesInitialized = true
    return true
}

let serviceDevicesInit(screen: UWord, storage: UWord, disk: UWord, imageBytes: UWord): Bool {
    if imageBytes == 0 || imageBytes & SECTOR_MASK != 0 return false
    if !deviceExtentInit(screen, storage, disk, imageBytes / SECTOR_SIZE, approvedStorageBytes()) return false
    approvedWritable = approvedStorageWritable()
    return true
}
let diskDevicesInit(owner: UWord, disk: UWord, imageBytes: UWord): Bool {
    return serviceDevicesInit(0, owner, disk, imageBytes)
}
// Boot policy for a trusted filesystem owner: the whole approved root, writable.
// It fails, with nothing adopted, when the root does not carry the writable bit.
let diskDevicesInitWritable(owner: UWord, disk: UWord, imageBytes: UWord): Bool {
    if !diskDevicesInit(owner, disk, imageBytes) return false
    if deviceExtentConfigure(owner, 0, approvedBytes, EXTENT_WRITE) != 0 {
        serviceDevicesRollback()
        return false
    }
    return true
}
let serviceDevicesRollback(): Void {
    objectAssertAtomic()
    if deviceBounce != PAGE_NONE return
    deviceDisk = null
    deviceExtent.owner = 0
    deviceExtent.writable = false
    deviceExtent.dirty = false
    deviceExtent.dirtyAtRegrant = false
    approvedWritable = false
    screenOwner = 0
    screenFixed = false
    devicesInitialized = false
}

let deviceMediumLive(): Bool {
    if deviceExtent.revoked || deviceDisk == null return false
    if deviceDisk.status & (DISK_PRESENT | DISK_CHANGED) != DISK_PRESENT {
        deviceExtent.revoked = true
        deviceExtent.mediumInvalid = true
        return false
    }
    return true
}
let deviceValidate(owner: UWord): Word {
    objectAssertAtomic()
    if owner == 0 || owner != deviceExtent.owner return -ERRNO_EPERM
    let task: *mut Task = taskGet(owner)
    if task == null || task.state == TASK_DEAD || !deviceMediumLive() return -ERRNO_EPIPE
    return 0
}
let diskInfo(owner: UWord): Word {
    let status: Word = deviceValidate(owner)
    if status != 0 return status
    return deviceExtent.bytes as Word
}
// Extent flags for the owner: EXTENT_WRITE only while the extent, the root and
// the drive all allow a write, EXTENT_DIRTY after a WRITE not yet flushed, and
// EXTENT_DIRTY_AT_REGRANT when the previous owner died with data unflushed.
let diskFlags(owner: UWord): Word {
    let status: Word = deviceValidate(owner)
    if status != 0 return status
    let mut flags: UWord = 0
    if deviceExtent.writable && deviceDisk.status & DISK_STATUS_READONLY == 0 flags |= EXTENT_WRITE
    if deviceExtent.dirty flags |= EXTENT_DIRTY
    if deviceExtent.dirtyAtRegrant flags |= EXTENT_DIRTY_AT_REGRANT
    return flags as Word
}

// Preflight before consuming an IRQ generation or mutating ownership.
let diskDevicesCheck(disk: UWord): Word {
    objectAssertAtomic()
    if serviceDiskIrq(disk) == DEVICE_NONE return -ERRNO_EPERM
    if !devicesInitialized {
        let drive: *volatile DiskRegisters = disk as *volatile DiskRegisters
        if drive.status & DISK_BUSY != 0 return -ERRNO_EBUSY
        return 0
    }
    let old: *mut Task = taskGet(deviceExtent.owner)
    if ((screenFixed && screenOwner != 0) || (old != null && old.state != TASK_DEAD)) return -ERRNO_EBUSY
    deviceReap()
    if deviceBounce != PAGE_NONE || deviceDisk == null || deviceDisk.status & DISK_BUSY != 0 return -ERRNO_EBUSY
    if deviceExtent.mediumInvalid || deviceDisk.status & (DISK_PRESENT | DISK_CHANGED) != DISK_PRESENT return -ERRNO_EPIPE
    if deviceDisk != disk as *volatile mut DiskRegisters return -ERRNO_EPERM
    if deviceExtent.generation == 0x7FFFFFFF return -ERRNO_EOVERFLOW
    return 0
}
let diskDevicesRegrant(owner: UWord, disk: UWord, imageBytes: UWord): Word {
    objectAssertAtomic()
    let child: *mut Task = taskGet(owner)
    if child == null || child.state != TASK_CREATED return -ERRNO_EPERM
    let checked: Word = diskDevicesCheck(disk)
    if checked != 0 return checked
    if !devicesInitialized {
        if !diskDevicesInit(owner, disk, imageBytes) return -ERRNO_EPIPE
        return 0
    }
    deviceDisk.status = DISK_DONE
    fence()
    deviceExtent.owner = owner
    deviceExtent.generation += 1
    deviceExtent.firstSector = approvedFirstSector
    deviceExtent.bytes = approvedBytes
    deviceExtent.revoked = false
    // A replacement never inherits write authority. An owner that died with
    // unflushed WRITEs leaves the medium in an unknown state; the manager sees
    // that flag and decides before it selects a writable extent again.
    if deviceExtent.dirty deviceExtent.dirtyAtRegrant = true
    deviceExtent.dirty = false
    deviceExtent.writable = false
    return 0
}

// Called only after scoped manager + child CONFIGURE authority is checked.
// Offset is relative to the immutable approved root, never an MMIO/PA value.
// Zero bytes select the rest of the approved root from `offset`, so a manager
// that never learns the root's size (init) can still grant all of it.
let deviceExtentConfigure(owner: UWord, offset: UWord, requested: UWord, flags: UWord): Word {
    objectAssertAtomic()
    if owner == 0 || owner != deviceExtent.owner return -ERRNO_EPERM
    let child: *mut Task = taskGet(owner)
    if child == null || child.state != TASK_CREATED || child.configured ||
        deviceBounce != PAGE_NONE || deviceExtent.revoked return -ERRNO_EBUSY
    if offset & SECTOR_MASK != 0 || offset >= approvedBytes return -ERRNO_EINVAL
    let mut bytes: UWord = requested
    if bytes == 0 bytes = approvedBytes - offset
    if bytes > approvedBytes - offset || flags & ~EXTENT_WRITE != 0 return -ERRNO_EINVAL
    // Write authority is a window of whole sectors inside a writable root.
    if flags & EXTENT_WRITE != 0 {
        if !approvedWritable return -ERRNO_EROFS
        if bytes & SECTOR_MASK != 0 return -ERRNO_EINVAL
    }
    if deviceExtent.generation == 0x7FFFFFFF return -ERRNO_EOVERFLOW
    deviceExtent.firstSector = approvedFirstSector + offset / SECTOR_SIZE
    deviceExtent.bytes = bytes
    deviceExtent.generation += 1
    deviceExtent.writable = flags & EXTENT_WRITE != 0
    // Selecting the extent is the manager's explicit decision after a dirty regrant.
    deviceExtent.dirtyAtRegrant = false
    return 0
}

// Sectors a nonempty byte range touches.
let deviceSpan(offset: UWord, bytes: UWord): UWord {
    return (offset % SECTOR_SIZE + bytes - 1) / SECTOR_SIZE + 1
}

// One bounded operation per physical engine, at most DISK_BOUNCE_SECTORS sectors
// through one pinned bounce page. READ may start and end anywhere in the extent.
// WRITE moves whole sectors from `source`, copied into the bounce before the
// command so no DMA reads user memory later. FLUSH carries no data. WRITE and
// FLUSH need the extent's write authority and a drive that is not READONLY.
let deviceSubmit(owner: UWord, offset: UWord, bytes: UWord, command: UWord, source: UWord): Word {
    let status: Word = deviceValidate(owner)
    if status != 0 return status
    if command != DISK_READ && command != DISK_WRITE && command != DISK_FLUSH return -ERRNO_EINVAL
    if command != DISK_READ && (!deviceExtent.writable || !approvedWritable ||
        deviceDisk.status & DISK_STATUS_READONLY != 0) return -ERRNO_EROFS
    let mut sectors: UWord = 0
    if command == DISK_FLUSH {
        if offset != 0 || bytes != 0 || source != 0 return -ERRNO_EINVAL
    } else {
        if bytes == 0 || offset >= deviceExtent.bytes || bytes > deviceExtent.bytes - offset return -ERRNO_EINVAL
        sectors = deviceSpan(offset, bytes)
        if sectors > DISK_BOUNCE_SECTORS return -ERRNO_EINVAL
        if command == DISK_READ {
            if source != 0 return -ERRNO_EINVAL
        } else if offset & SECTOR_MASK != 0 || bytes & SECTOR_MASK != 0 || source == 0 return -ERRNO_EINVAL
    }
    if deviceBounce != PAGE_NONE || deviceDisk.status & DISK_BUSY != 0 return -ERRNO_EBUSY
    if deviceOperation.instance == 0x7FFFFFFF return -ERRNO_EOVERFLOW
    let sector: UWord = deviceExtent.firstSector + offset / SECTOR_SIZE
    if command != DISK_FLUSH && (sector >= deviceDisk.sectors || sectors > deviceDisk.sectors - sector) return -ERRNO_EPIPE
    deviceBounce = allocPage(DMA_BUFFER_OWNER, PAGE_KERNEL)
    if deviceBounce == PAGE_NONE return -ERRNO_EBUSY
    if !physicalPageOwned(deviceBounce, DMA_BUFFER_OWNER, PAGE_KERNEL) ||
        deviceBounce % PAGE_SIZE != 0 || DISK_BOUNCE_SECTORS * SECTOR_SIZE > PAGE_SIZE ||
        !retainPage(deviceBounce, DMA_BUFFER_OWNER, PAGE_KERNEL) {
        if !freePage(deviceBounce, DMA_BUFFER_OWNER, PAGE_KERNEL) panic("invalid DMA reservation", null)
        deviceBounce = PAGE_NONE
        return -ERRNO_EIO
    }
    let buffer: *mut UWord = deviceBounce as *mut UWord
    for i: UWord in 0..(PAGE_SIZE / 4) buffer[i] = 0
    if command == DISK_WRITE {
        let task: *mut Task = taskGet(owner)
        let copied: Word = copyFromUser(task.directory, owner, deviceBounce as *mut UByte, source, bytes)
        if copied != 0 {
            // Nothing was issued: the page is released and the extent stays clean.
            deviceReleaseBuffer()
            return copied
        }
    }
    deviceOperation.owner = owner
    deviceOperation.instance += 1
    deviceOperation.resourceGeneration = deviceExtent.generation
    deviceOperation.offset = offset % SECTOR_SIZE
    deviceOperation.bytes = bytes
    deviceOperation.command = command
    deviceOperation.cancelled = false
    deviceDisk.sector = sector
    deviceDisk.count = sectors
    deviceDisk.address = deviceBounce
    if command == DISK_WRITE deviceExtent.dirty = true
    fence()
    deviceDisk.command = command
    fence()
    return deviceOperation.instance as Word
}
let deviceReleaseBuffer(): Void {
    if deviceBounce == PAGE_NONE return
    if !releasePage(deviceBounce, DMA_BUFFER_OWNER, PAGE_KERNEL) ||
        !freePage(deviceBounce, DMA_BUFFER_OWNER, PAGE_KERNEL) {
        panic("could not release completed device DMA", null)
        return
    }
    deviceBounce = PAGE_NONE
}
let deviceCancelOwner(owner: UWord): Void {
    objectAssertAtomic()
    if owner == 0 || owner != deviceExtent.owner return
    deviceExtent.revoked = true
    if deviceOperation.owner == owner deviceOperation.cancelled = true
    deviceReap()
}
let deviceCancel(owner: UWord, instance: UWord): Word {
    objectAssertAtomic()
    if owner == 0 || owner != deviceExtent.owner return -ERRNO_EPERM
    if deviceBounce == PAGE_NONE || instance != deviceOperation.instance ||
        owner != deviceOperation.owner return -ERRNO_EINVAL
    deviceCancelOwner(owner)
    return 0
}
// Cancellation is logical. Masking/death/timeout cannot abort WRM DMA. A
// cancelled page remains pinned until BUSY clears, including permanent hangs.
let deviceReap(): Void {
    objectAssertAtomic()
    if deviceBounce == PAGE_NONE || !deviceOperation.cancelled || deviceDisk == null ||
        deviceDisk.status & DISK_BUSY != 0 return
    fence()
    deviceDisk.status = DISK_DONE
    fence()
    deviceReleaseBuffer()
}
let serviceDevicesQuiescent(owner: UWord): Bool {
    objectAssertAtomic()
    if owner != deviceExtent.owner return true
    deviceReap()
    return deviceBounce == PAGE_NONE
}
let deviceFinish(owner: UWord, instance: UWord, destination: UWord): Word {
    objectAssertAtomic()
    if owner == 0 || owner != deviceExtent.owner return -ERRNO_EPERM
    if deviceBounce == PAGE_NONE || deviceOperation.owner != owner ||
        instance != deviceOperation.instance || deviceOperation.cancelled ||
        deviceOperation.resourceGeneration != deviceExtent.generation return -ERRNO_EINVAL
    let state: UWord = deviceDisk.status
    if state & DISK_BUSY != 0 || state & DISK_DONE == 0 {
        deviceCancelOwner(owner)
        return -ERRNO_EIO
    }
    fence()
    let mut result: Word = -ERRNO_EIO
    if deviceValidate(owner) != 0 result = -ERRNO_EPIPE
    else if deviceDisk.error == 0 {
        if deviceOperation.command == DISK_READ {
            let task: *mut Task = taskGet(owner)
            result = copyToUser(task.directory, owner, destination,
                (deviceBounce + deviceOperation.offset) as *UByte, deviceOperation.bytes)
            if result == 0 result = deviceOperation.bytes as Word
        } else {
            // A WRITE returns the bytes the device took; a FLUSH makes the
            // extent clean. A device error leaves it dirty.
            result = deviceOperation.bytes as Word
            if deviceOperation.command == DISK_FLUSH deviceExtent.dirty = false
        }
    } else if deviceDisk.error == DISK_ERROR_READONLY result = -ERRNO_EROFS
    deviceDisk.status = DISK_DONE
    fence()
    deviceReleaseBuffer()
    return result
}

// Safe register broker. Mode, palette and scanout policy belong to the owner.
// DMA/drawing registers and arbitrary command/control bits are never writable.
let displayFrameBytes(mode: UWord): UWord {
    if mode & 0xFFFFFF8C != 0 || mode >> 4 > 4 return 0
    let mut pixels: UWord = 320 * 240
    if mode & 3 == 1 pixels = 640 * 480
    else if mode & 3 == 2 pixels = 800 * 600
    else if mode & 3 == 3 pixels = 1024 * 768
    let depth: UWord = mode >> 4
    if depth == 0 return pixels / 8
    if depth == 1 return pixels / 2
    return pixels << (depth - 2)
}
// A frame fits when its size is valid and lies wholly inside the granted VRAM window.
let displayFrameFits(mode: UWord, start: UWord, vram: UWord): Bool {
    let bytes: UWord = displayFrameBytes(mode)
    return bytes != 0 && start <= vram && bytes <= vram - start
}
let screenControl(owner: UWord, register: UWord, value: UWord): Word {
    objectAssertAtomic()
    let task: *mut Task = taskGet(owner)
    if owner == 0 || owner != screenOwner || task == null || task.state == TASK_DEAD return -ERRNO_EPERM
    let rule: UWord = deviceRegisterRule(register)
    let base: UWord = deviceRoleRegisters(DEVICE_ROLE_SCREEN)
    let vram: UWord = deviceRoleVramBytes(DEVICE_ROLE_SCREEN)
    if rule == REGISTER_RULE_COUNT || base == 0 || vram == 0 return -ERRNO_EINVAL
    let registers: *volatile mut UWord = base as *volatile mut UWord
    if deviceRuleFlags(rule) & REGISTER_IDLE != 0 && registers[0] & VIDEO_BUSY != 0 return -ERRNO_EBUSY
    if value & ~deviceRuleMask(rule) != 0 return -ERRNO_EINVAL
    let check: UWord = deviceRuleCheck(rule)
    if check == CHECK_ENABLE {
        if value & 1 != 0 && !displayFrameFits(registers[2], registers[8], vram) return -ERRNO_EINVAL
    } else if check == CHECK_MODE {
        if !displayFrameFits(value, registers[8], vram) return -ERRNO_EINVAL
    } else if check == CHECK_START {
        if !displayFrameFits(registers[2], value, vram) return -ERRNO_EINVAL
    }
    registers[register / 4] = value
    fence()
    return 0
}
// Runtime Screen handover. Screen holds no DMA authority, so the preflight has
// no pin to wait for: the old owner must be gone (its death cleared the owner and
// stopped scanout), its directory must be destroyed (the device-table ranges are
// free in the resource ledger), and the engine must not be mid-command. Nothing
// is mutated by a failed check. The video engine is left in the state the dead
// owner's release put it in; the new owner reprograms it through screenControl.
let screenDevicesCheck(): Word {
    objectAssertAtomic()
    let base: UWord = deviceRoleRegisters(DEVICE_ROLE_SCREEN)
    let rows: UWord = deviceRoleMappingCount(DEVICE_ROLE_SCREEN)
    if screenFixed || base == 0 || rows == 0 return -ERRNO_EPERM
    if screenOwner != 0 return -ERRNO_EBUSY
    for index: UWord in 0..rows {
        let row: UWord = deviceRoleMapping(DEVICE_ROLE_SCREEN, index)
        if row == DEVICE_ROW_COUNT return -ERRNO_EPERM
        if !mmuResourceFree(deviceRowPhysical(row)) return -ERRNO_EBUSY
    }
    let registers: *volatile UWord = base as *volatile UWord
    if registers[0] & VIDEO_BUSY != 0 return -ERRNO_EBUSY
    return 0
}
// Installs every mapping row of the Screen role into an unpublished child and
// records it as the display owner. A mapping failure after the check (page-table
// exhaustion) leaves the child unusable: it holds no owner, no rights and must
// be discarded by its supervisor.
let screenDevicesRegrant(owner: UWord): Word {
    objectAssertAtomic()
    let child: *mut Task = taskGet(owner)
    if child == null || child.state != TASK_CREATED return -ERRNO_EPERM
    let checked: Word = screenDevicesCheck()
    if checked != 0 return checked
    let rows: UWord = deviceRoleMappingCount(DEVICE_ROLE_SCREEN)
    for index: UWord in 0..rows {
        let row: UWord = deviceRoleMapping(DEVICE_ROLE_SCREEN, index)
        if !mmuInstallResource(child.directory, owner, deviceRowVirtual(row), deviceRowPhysical(row),
            deviceRowBytes(row), deviceRowPermissions(row)) return -ERRNO_ENFILE
    }
    screenOwner = owner
    return 0
}
let screenReleaseOwner(owner: UWord): Void {
    objectAssertAtomic()
    if owner == 0 || owner != screenOwner return
    let registers: *volatile mut UWord = deviceRoleRegisters(DEVICE_ROLE_SCREEN) as *volatile mut UWord
    registers[1] = 0
    registers[0] = 10
    fence()
    screenOwner = 0
}
export { DeviceExtent, DeviceOperation, serviceDiskIrq, serviceDevicesInit,
    deviceExtentInit, deviceExtentConfigure, serviceDevicesRollback,
    diskDevicesCheck, diskDevicesRegrant, diskDevicesInit, diskDevicesInitWritable, diskInfo, diskFlags,
    serviceDevicesQuiescent, deviceSubmit, deviceFinish, deviceCancel,
    deviceCancelOwner, deviceReap, screenControl, screenReleaseOwner,
    screenDevicesCheck, screenDevicesRegrant }

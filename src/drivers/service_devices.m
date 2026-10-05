// Trusted physical-device mechanism. Descriptors contain approved byte ranges;
// no filesystem, bitmap format, user address or user command reaches DMA MMIO.
import { DISK0_BASE, DISK1_BASE, FLOPPY_BASE, DISK_PRESENT, DISK_CHANGED,
    DISK_BUSY, DISK_DONE, DISK_READ, SECTOR_SIZE, SECTOR_MASK, PAGE_SIZE,
    VIDEO_BASE, VIDEO_BUSY, SCREEN_VRAM_BYTES, ERRNO_EPERM, ERRNO_EINVAL,
    ERRNO_EBUSY, ERRNO_EPIPE, ERRNO_EIO, ERRNO_EOVERFLOW } from "../arch/wrm081632/defs.m"
import { PAGE_NONE, PAGE_KERNEL, allocPage, freePage, retainPage, releasePage,
    physicalPageOwned } from "../mm/memory.m"
import { copyToUser } from "../mm/mmu.m"
import { taskGet, Task, TASK_CREATED, TASK_DEAD } from "../task/task.m"
import { approvedStorageBytes } from "resources.m"
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
}

type DeviceOperation {
    owner: UWord, // immutable full task reference, including slot generation
    instance: UWord, // monotonic, never wraps or resets on regrant
    resourceGeneration: UWord,
    offset: UWord,
    bytes: UWord,
    cancelled: Bool,
}
let DMA_BUFFER_OWNER: UWord = 0xFFFFFFFE
let mut deviceDisk: *volatile mut DiskRegisters
let mut deviceExtent: DeviceExtent
let mut deviceOperation: DeviceOperation
let mut deviceBounce: UWord
let mut screenOwner: UWord
let mut approvedFirstSector: UWord
let mut approvedBytes: UWord
let mut devicesInitialized: Bool

let serviceDiskIrq(disk: UWord): UWord {
    if disk == DISK0_BASE return 3
    if disk == DISK1_BASE return 4
    if disk == FLOPPY_BASE return 6
    return 32
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
        storageTask.state != TASK_CREATED || serviceDiskIrq(disk) == 32 ||
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
    screenOwner = screen
    devicesInitialized = true
    return true
}

let serviceDevicesInit(screen: UWord, storage: UWord, disk: UWord, imageBytes: UWord): Bool {
    if imageBytes == 0 || imageBytes & SECTOR_MASK != 0 return false
    return deviceExtentInit(screen, storage, disk, imageBytes / SECTOR_SIZE, approvedStorageBytes)
}
let diskDevicesInit(owner: UWord, disk: UWord, imageBytes: UWord): Bool {
    return serviceDevicesInit(0, owner, disk, imageBytes)
}
let serviceDevicesRollback(): Void {
    objectAssertAtomic()
    if deviceBounce != PAGE_NONE return
    deviceDisk = null
    deviceExtent.owner = 0
    screenOwner = 0
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

// Preflight before consuming an IRQ generation or mutating ownership.
let diskDevicesCheck(disk: UWord): Word {
    objectAssertAtomic()
    if serviceDiskIrq(disk) == 32 return -ERRNO_EPERM
    if !devicesInitialized {
        let drive: *volatile DiskRegisters = disk as *volatile DiskRegisters
        if drive.status & DISK_BUSY != 0 return -ERRNO_EBUSY
        return 0
    }
    let old: *mut Task = taskGet(deviceExtent.owner)
    if screenOwner != 0 || (old != null && old.state != TASK_DEAD) return -ERRNO_EBUSY
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
    return 0
}

// Called only after scoped manager + child CONFIGURE authority is checked.
// Offset is relative to the immutable approved root, never an MMIO/PA value.
let deviceExtentConfigure(owner: UWord, offset: UWord, bytes: UWord): Word {
    objectAssertAtomic()
    if owner == 0 || owner != deviceExtent.owner return -ERRNO_EPERM
    let child: *mut Task = taskGet(owner)
    if child == null || child.state != TASK_CREATED || child.configured ||
        deviceBounce != PAGE_NONE || deviceExtent.revoked return -ERRNO_EBUSY
    if offset & SECTOR_MASK != 0 || bytes == 0 || offset >= approvedBytes ||
        bytes > approvedBytes - offset return -ERRNO_EINVAL
    if deviceExtent.generation == 0x7FFFFFFF return -ERRNO_EOVERFLOW
    deviceExtent.firstSector = approvedFirstSector + offset / SECTOR_SIZE
    deviceExtent.bytes = bytes
    deviceExtent.generation += 1
    return 0
}

// One bounded operation per physical engine; the physical transfer is one
// sector, while publication may copy any checked subrange of that sector.
let deviceSubmit(owner: UWord, offset: UWord, bytes: UWord, command: UWord): Word {
    let status: Word = deviceValidate(owner)
    if status != 0 return status
    if command != DISK_READ || bytes == 0 || bytes > SECTOR_SIZE ||
        offset >= deviceExtent.bytes || bytes > deviceExtent.bytes - offset ||
        bytes > SECTOR_SIZE - offset % SECTOR_SIZE return -ERRNO_EINVAL
    if deviceBounce != PAGE_NONE || deviceDisk.status & DISK_BUSY != 0 return -ERRNO_EBUSY
    if deviceOperation.instance == 0x7FFFFFFF return -ERRNO_EOVERFLOW
    let sector: UWord = deviceExtent.firstSector + offset / SECTOR_SIZE
    if sector >= deviceDisk.sectors return -ERRNO_EPIPE
    deviceBounce = allocPage(DMA_BUFFER_OWNER, PAGE_KERNEL)
    if deviceBounce == PAGE_NONE return -ERRNO_EBUSY
    if !physicalPageOwned(deviceBounce, DMA_BUFFER_OWNER, PAGE_KERNEL) ||
        deviceBounce % PAGE_SIZE != 0 || SECTOR_SIZE > PAGE_SIZE ||
        !retainPage(deviceBounce, DMA_BUFFER_OWNER, PAGE_KERNEL) {
        if !freePage(deviceBounce, DMA_BUFFER_OWNER, PAGE_KERNEL) panic("invalid DMA reservation", null)
        deviceBounce = PAGE_NONE
        return -ERRNO_EIO
    }
    let buffer: *mut UWord = deviceBounce as *mut UWord
    for i: UWord in 0..(SECTOR_SIZE / 4) buffer[i] = 0
    deviceOperation.owner = owner
    deviceOperation.instance += 1
    deviceOperation.resourceGeneration = deviceExtent.generation
    deviceOperation.offset = offset % SECTOR_SIZE
    deviceOperation.bytes = bytes
    deviceOperation.cancelled = false
    deviceDisk.sector = sector
    deviceDisk.count = 1
    deviceDisk.address = deviceBounce
    fence()
    deviceDisk.command = DISK_READ
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
        let task: *mut Task = taskGet(owner)
        result = copyToUser(task.directory, owner, destination,
            (deviceBounce + deviceOperation.offset) as *UByte, deviceOperation.bytes)
        if result == 0 result = deviceOperation.bytes as Word
    }
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
let screenControl(owner: UWord, register: UWord, value: UWord): Word {
    objectAssertAtomic()
    let task: *mut Task = taskGet(owner)
    if owner == 0 || owner != screenOwner || task == null || task.state == TASK_DEAD return -ERRNO_EPERM
    if register != 0 && register != 4 && register != 8 && register != 32 &&
        register != 40 && register != 44 && register != 128 return -ERRNO_EINVAL
    let registers: *volatile mut UWord = VIDEO_BASE as *volatile mut UWord
    if register == 0 {
        if value & 0xFFFFFFF5 != 0 return -ERRNO_EINVAL
    } else {
        if registers[0] & VIDEO_BUSY != 0 return -ERRNO_EBUSY
        if register == 4 {
            if value & 0xFFFFFFFA != 0 return -ERRNO_EINVAL
            if value & 1 != 0 {
                let bytes: UWord = displayFrameBytes(registers[2])
                let start: UWord = registers[8]
                if bytes == 0 || start > SCREEN_VRAM_BYTES || bytes > SCREEN_VRAM_BYTES - start return -ERRNO_EINVAL
            }
        } else if register == 8 || register == 32 {
            let mut mode: UWord = registers[2]
            let mut start: UWord = registers[8]
            if register == 8 mode = value
            else start = value
            let bytes: UWord = displayFrameBytes(mode)
            if bytes == 0 || start > SCREEN_VRAM_BYTES || bytes > SCREEN_VRAM_BYTES - start return -ERRNO_EINVAL
        } else if register == 40 {
            if value >= 256 return -ERRNO_EINVAL
        } else if register == 44 {
            if value > 0xFFFFFF return -ERRNO_EINVAL
        } else if register == 128 {
            if value != 0 return -ERRNO_EINVAL // cursor stays off until bounded sprite grants exist
        } else return -ERRNO_EINVAL
    }
    registers[register / 4] = value
    fence()
    return 0
}
let screenReleaseOwner(owner: UWord): Void {
    objectAssertAtomic()
    if owner == 0 || owner != screenOwner return
    let registers: *volatile mut UWord = VIDEO_BASE as *volatile mut UWord
    registers[1] = 0
    registers[0] = 10
    fence()
    screenOwner = 0
}
export { DeviceExtent, DeviceOperation, serviceDiskIrq, serviceDevicesInit,
    deviceExtentInit, deviceExtentConfigure, serviceDevicesRollback,
    diskDevicesCheck, diskDevicesRegrant, diskDevicesInit, diskInfo,
    serviceDevicesQuiescent, deviceSubmit, deviceFinish, deviceCancel,
    deviceCancelOwner, deviceReap, screenControl, screenReleaseOwner }

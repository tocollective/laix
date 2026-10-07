// Byte chunks in the approved byte resource, plus a 512-byte sector stage for the
// writable block path. No raw DMA address. Reads work on any extent; the stage
// operations (LOAD, STORE, SYNC) reach the kernel's write contract and fail with
// EROFS unless the manager made the extent writable.
import { ServiceStart, serviceStartValid } from "../../src/task/service_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_DISK, DATA_GENERATION,
    DISK_REQUEST_HEADER, DISK_STAT_HEADER, DISK_RESPONSE_HEADER, DISK_LOAD_HEADER,
    DISK_PEEK_HEADER, DISK_POKE_HEADER, DISK_STORE_HEADER, DISK_SYNC_HEADER, DISK_FLAGS_HEADER,
    DISK_STAGE_BYTES, SECTOR_SIZE, SECTOR_MASK,
    ERRNO_EINVAL, ERRNO_EPIPE, ERRNO_EIO } from "../../src/arch/wrm081632/defs.m"
import { AcceptResult, accept, reply, exit, irqWait, irqComplete,
    diskInfo, diskFlags, diskBegin, diskWriteBegin, diskFlushBegin, diskFinish, diskCancel } from "../syscalls.m"
let mut diskRequest: UWord[8]
let mut diskResponse: UWord[8]
// One sector in flight between the filesystem and the device. Its only client
// is the filesystem, so there is no per-client staging.
let mut diskStage: UByte[512]
let mut diskGeneration: UWord = DATA_GENERATION
let mut diskFailed: Bool

// Waits for an operation begun with a positive instance, then collects it.
// Returns the Finish result after re-arming the IRQ, or a negative errno. A
// timeout cancels the operation; the kernel keeps its page pinned until the
// device is idle.
let diskAwait(instance: UWord, irq: UWord, destination: *mut UByte): Word {
    if irqWait(irq, 5) != 0 {
        if diskCancel(instance) != 0 exit(1)
        return -ERRNO_EIO
    }
    let result: Word = diskFinish(instance, destination)
    if result < 0 return result
    let completed: Word = irqComplete(irq)
    if completed != 0 return completed
    return result
}

// LOAD: the sector at `offset` into the stage. The last sector of an extent that
// is not a whole number of sectors is zero padded.
let diskLoad(offset: UWord, extent: UWord, irq: UWord): Word {
    if offset & SECTOR_MASK != 0 || offset >= extent return -ERRNO_EINVAL
    let mut count: UWord = SECTOR_SIZE
    if count > extent - offset count = extent - offset
    for i: UWord in 0..DISK_STAGE_BYTES diskStage[i] = 0
    let began: Word = diskBegin(offset, count)
    if began < 0 return began
    if began == 0 return -ERRNO_EIO
    let result: Word = diskAwait(began as UWord, irq, &mut diskStage[0] as *mut UByte)
    if result < 0 return result
    let got: UWord = result as UWord
    if got != count return -ERRNO_EIO
    return count as Word
}

// STORE: the whole stage to the sector at `offset`. The kernel refuses with
// EROFS unless root, extent and drive all allow writing.
let diskStore(offset: UWord, extent: UWord, irq: UWord): Word {
    if offset & SECTOR_MASK != 0 || offset >= extent || extent - offset < SECTOR_SIZE return -ERRNO_EINVAL
    let began: Word = diskWriteBegin(offset, SECTOR_SIZE, &diskStage[0] as *UByte)
    if began < 0 return began
    if began == 0 return -ERRNO_EIO
    let result: Word = diskAwait(began as UWord, irq, &mut diskStage[0] as *mut UByte)
    if result < 0 return result
    let got: UWord = result as UWord
    if got != SECTOR_SIZE return -ERRNO_EIO
    return 0
}

// SYNC: every earlier STORE has reached the medium when this returns zero.
let diskSync(irq: UWord): Word {
    let began: Word = diskFlushBegin()
    if began < 0 return began
    if began == 0 return -ERRNO_EIO
    let result: Word = diskAwait(began as UWord, irq, &mut diskStage[0] as *mut UByte)
    if result < 0 return result
    return 0
}

// PEEK and POKE move 16 bytes of the stage and never touch the device.
let diskStageMove(request: *UWord, response: *mut UWord, poke: Bool): Void {
    if request[3] != 0 || request[2] & 15 != 0 || request[2] >= DISK_STAGE_BYTES return
    if poke {
        let source: *UByte = &request[4] as *UByte
        for i: UWord in 0..16 diskStage[request[2] + i] = source[i]
    } else {
        let destination: *mut UByte = &mut response[4] as *mut UByte
        for i: UWord in 0..16 destination[i] = diskStage[request[2] + i]
        response[3] = 16
    }
    response[1] = 0
}

let diskHandle(request: *UWord, size: UWord, response: *mut UWord, irq: UWord): Void {
    for i: UWord in 0..8 response[i] = 0
    response[0] = DISK_RESPONSE_HEADER
    response[1] = (-ERRNO_EINVAL) as UWord
    response[2] = diskGeneration
    let header: UWord = request[0]
    let poke: Bool = size == 32 && header == DISK_POKE_HEADER
    let plain: Bool = size == 16 && (header == DISK_REQUEST_HEADER || header == DISK_STAT_HEADER ||
        header == DISK_LOAD_HEADER || header == DISK_PEEK_HEADER || header == DISK_STORE_HEADER ||
        header == DISK_SYNC_HEADER || header == DISK_FLAGS_HEADER)
    if !poke && !plain return
    if request[1] != diskGeneration {
        response[1] = (-ERRNO_EPIPE) as UWord
        return
    }
    if header == DISK_PEEK_HEADER || poke {
        diskStageMove(request, response, poke)
        return
    }
    let unary: Bool = header == DISK_STAT_HEADER || header == DISK_SYNC_HEADER || header == DISK_FLAGS_HEADER
    if unary && (request[2] != 0 || request[3] != 0) return
    let mut status: Word = diskInfo()
    if status < 0 {
        response[1] = status as UWord
        if status == -ERRNO_EPIPE || status == -ERRNO_EIO diskFailed = true
        return
    }
    if header == DISK_STAT_HEADER {
        response[1] = 0
        response[3] = status as UWord
        return
    }
    if header == DISK_FLAGS_HEADER {
        let flags: Word = diskFlags()
        response[1] = flags as UWord
        if flags >= 0 {
            response[1] = 0
            response[3] = flags as UWord
        } else if flags == -ERRNO_EPIPE || flags == -ERRNO_EIO diskFailed = true
        return
    }
    if header == DISK_LOAD_HEADER || header == DISK_STORE_HEADER || header == DISK_SYNC_HEADER {
        if request[3] != 0 return
        let extent: UWord = status as UWord
        if header == DISK_LOAD_HEADER status = diskLoad(request[2], extent, irq)
        else if header == DISK_STORE_HEADER status = diskStore(request[2], extent, irq)
        else status = diskSync(irq)
        response[1] = status as UWord
        if status > 0 {
            response[1] = 0
            response[3] = status as UWord
        }
        if status == -ERRNO_EPIPE || status == -ERRNO_EIO diskFailed = true
        return
    }
    if request[3] == 0 || request[3] > 16 return
    status = diskBegin(request[2], request[3])
    if status > 0 {
        let instance: UWord = status as UWord
        status = irqWait(irq, 5)
        if status != 0 {
            if diskCancel(instance) != 0 exit(1)
            status = -ERRNO_EIO
        } else {
            status = diskFinish(instance, &mut response[4] as *mut UByte)
            if status > 0 && (status as UWord) == request[3] {
                response[3] = status as UWord
                status = irqComplete(irq)
            } else if status >= 0 status = -ERRNO_EIO
        }
    }
    if status != 0 {
        response[3] = 0
        for i: UWord in 4..8 response[i] = 0
    }
    response[1] = status as UWord
    if status == -ERRNO_EPIPE || status == -ERRNO_EIO diskFailed = true
}

let diskMain(start: *ServiceStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != SERVICE_START_BYTES ||
        !serviceStartValid(start) || start.role != START_ROLE_DISK) exit(1)
    if irqComplete(start.irq) != 0 exit(1)
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(start.endpoint, &mut diskRequest[0] as *mut UByte, 32, &mut accepted)
        if size < 0 exit(1)
        diskHandle(&diskRequest[0], size as UWord, &mut diskResponse[0], start.irq)
        let sent: Word = reply(accepted.replyToken, &diskResponse[0] as *UByte, 32)
        if sent > 32 exit(1)
        if diskFailed exit(1)
    }
}
let diskSetGeneration(generation: UWord): Void { diskGeneration = generation }
let diskDependencyFailed(): Bool { return diskFailed }
export { diskDependencyFailed, diskSetGeneration, diskHandle, diskMain }

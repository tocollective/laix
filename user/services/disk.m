// Read-only byte chunks in the fixed boot bitmap extent. No raw DMA address.
import { ServiceStart, serviceStartValid } from "../../src/task/service_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_DISK, DATA_GENERATION,
    DISK_REQUEST_HEADER, DISK_STAT_HEADER, DISK_RESPONSE_HEADER,
    ERRNO_EINVAL, ERRNO_EPIPE, ERRNO_EIO } from "../../src/arch/wrm081632/defs.m"
import { AcceptResult, accept, reply, exit, irqWait, irqComplete,
    diskInfo, diskBegin, diskFinish, diskCancel } from "../syscalls.m"
let mut diskRequest: UWord[8]
let mut diskResponse: UWord[8]
let mut diskGeneration: UWord = DATA_GENERATION
let mut diskFailed: Bool

let diskHandle(request: *UWord, size: UWord, response: *mut UWord, irq: UWord): Void {
    for i: UWord in 0..8 response[i] = 0
    response[0] = DISK_RESPONSE_HEADER
    response[1] = (-ERRNO_EINVAL) as UWord
    response[2] = diskGeneration
    if size != 16 || (request[0] != DISK_REQUEST_HEADER && request[0] != DISK_STAT_HEADER) return
    if request[1] != diskGeneration {
        response[1] = (-ERRNO_EPIPE) as UWord
        return
    }
    if request[0] == DISK_STAT_HEADER && (request[2] != 0 || request[3] != 0) return
    let mut status: Word = diskInfo()
    if status < 0 {
        response[1] = status as UWord
        if status == -ERRNO_EPIPE || status == -ERRNO_EIO diskFailed = true
        return
    }
    if request[0] == DISK_STAT_HEADER {
        response[1] = 0
        response[3] = status as UWord
        return
    }
    status = diskBegin(request[2], request[3])
    if status == 0 {
        status = irqWait(irq, 5)
        if status != 0 {
            if diskCancel() != 0 exit(1)
            status = -ERRNO_EIO
        } else {
            status = diskFinish(&mut response[4] as *mut UByte)
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

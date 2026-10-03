// Untrusted bitmap storage service: protocol parsing and replies in user mode;
// fixed-range physical disk DMA is exclusively the narrow kernel broker.
import { ServiceStart, serviceStartValid } from "../../src/task/service_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_STORAGE,
    FONT_GENERATION, FONT_REQUEST_HEADER, FONT_VALIDATE_HEADER,
    FONT_RESPONSE_HEADER, ERRNO_EINVAL, ERRNO_EPIPE, ERRNO_EIO } from "../../src/arch/wrm081632/defs.m"
import { AcceptResult, accept, reply, exit, irqWait, irqComplete,
    fontValidate, fontBegin, fontFinish, fontCancel } from "../syscalls.m"

let mut storageRequest: UWord[8]
let mut storageResponse: UWord[8]

let storageHandle(request: *UWord, size: UWord, response: *mut UWord, irq: UWord): Void {
    for i: UWord in 0..8 response[i] = 0
    response[0] = FONT_RESPONSE_HEADER
    response[1] = (-ERRNO_EINVAL) as UWord
    response[2] = FONT_GENERATION
    if size != 16 || (request[0] != FONT_REQUEST_HEADER && request[0] != FONT_VALIDATE_HEADER) return
    if request[1] != FONT_GENERATION {
        response[1] = (-ERRNO_EPIPE) as UWord
        return
    }
    if request[0] == FONT_VALIDATE_HEADER && (request[2] != 0 || request[3] != 0) return
    let mut status: Word = fontValidate()
    if status == 0 && request[0] == FONT_REQUEST_HEADER {
        status = fontBegin(request[2], request[3])
        if status == 0 {
            status = irqWait(irq, 5)
            if status != 0 {
                if fontCancel() != 0 exit(1)
                status = -ERRNO_EIO
            } else {
                status = fontFinish(&mut response[4] as *mut UByte)
                if status == 16 {
                    response[3] = 16
                    status = irqComplete(irq)
                }
            }
        }
    }
    if status != 0 {
        response[3] = 0
        for i: UWord in 4..8 response[i] = 0
    }
    response[1] = status as UWord
}

let storageMain(start: *ServiceStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != SERVICE_START_BYTES ||
        !serviceStartValid(start) || start.role != START_ROLE_STORAGE) exit(1)
    if irqComplete(start.irq) != 0 exit(1)
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(start.endpoint, &mut storageRequest[0] as *mut UByte, 32, &mut accepted)
        if size < 0 exit(1)
        storageHandle(&storageRequest[0], size as UWord, &mut storageResponse[0], start.irq)
        let sent: Word = reply(accepted.replyToken, &storageResponse[0] as *UByte, 32)
        if sent > 32 exit(1)
        // A revoked medium or failed/late DMA is terminal for this generation.
        if ((storageResponse[1] as Word) == -ERRNO_EPIPE ||
            (storageResponse[1] as Word) == -ERRNO_EIO) exit(1)
    }
}

export { storageMain, storageHandle }

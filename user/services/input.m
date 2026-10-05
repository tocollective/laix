// Nonblocking HID event requests; the service sleeps in accept when idle.
import { ServiceStart, serviceStartValid } from "../../src/task/service_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_INPUT, INPUT_REQUEST_HEADER,
    INPUT_RESPONSE_HEADER, ERRNO_EINVAL } from "../../src/arch/wrm081632/defs.m"
import { AcceptResult, accept, reply, exit, inputRead } from "../syscalls.m"
let mut inputRequest: UWord[8]
let mut inputResponse: UWord[8]

// Delivery and buffering policy can change independently of the raw broker.
let mut inputEvents: UWord[32]
let mut inputHead: UWord
let mut inputCount: UWord
let mut inputOverflow: Bool
let mut inputBatch: UWord[34]
let inputSnapshot(destination: *mut UWord): Word {
    let result: Word = inputRead(&mut inputBatch[0] as *mut UByte, 32)
    if result != 136 return result
    if inputBatch[0] > 32 return -ERRNO_EINVAL
    if inputBatch[1] != 0 inputOverflow = true
    for i: UWord in 0..inputBatch[0] {
        if inputCount == 32 inputOverflow = true
        else {
            inputEvents[(inputHead + inputCount) % 32] = inputBatch[2 + i]
            inputCount += 1
        }
    }
    let mut count: UWord = inputCount
    if count > 4 count = 4
    for i: UWord in 0..6 destination[i] = 0
    destination[0] = count
    if inputOverflow destination[1] = 1
    for i: UWord in 0..count destination[2 + i] = inputEvents[(inputHead + i) % 32]
    inputHead = (inputHead + count) % 32
    inputCount -= count
    inputOverflow = false
    return 24
}

let inputHandle(request: *UWord, size: UWord, response: *mut UWord): Void {
    for i: UWord in 0..8 response[i] = 0
    response[0] = INPUT_RESPONSE_HEADER
    response[1] = (-ERRNO_EINVAL) as UWord
    if size != 4 || request[0] != INPUT_REQUEST_HEADER return
    let result: Word = inputSnapshot(&mut response[2])
    if result == 24 response[1] = 0
    else response[1] = result as UWord
}

let inputMain(start: *ServiceStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != SERVICE_START_BYTES ||
        !serviceStartValid(start) || start.role != START_ROLE_INPUT) exit(1)
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(start.endpoint, &mut inputRequest[0] as *mut UByte, 32, &mut accepted)
        if size < 0 exit(1)
        inputHandle(&inputRequest[0], size as UWord, &mut inputResponse[0])
        let sent: Word = reply(accepted.replyToken, &inputResponse[0] as *UByte, 32)
        if sent > 32 exit(1)
    }
}
export { inputSnapshot, inputHandle, inputMain }

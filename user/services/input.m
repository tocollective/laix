// Nonblocking HID event requests; the service sleeps in accept when idle.
import { ServiceStart, serviceStartValid } from "../../src/task/service_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_INPUT, INPUT_REQUEST_HEADER,
    INPUT_RESPONSE_HEADER, ERRNO_EINVAL } from "../../src/arch/wrm081632/defs.m"
import { AcceptResult, accept, reply, exit, inputRead } from "../syscalls.m"
let mut inputRequest: UWord[8]
let mut inputResponse: UWord[8]

let inputHandle(request: *UWord, size: UWord, response: *mut UWord): Void {
    for i: UWord in 0..8 response[i] = 0
    response[0] = INPUT_RESPONSE_HEADER
    response[1] = (-ERRNO_EINVAL) as UWord
    if size != 4 || request[0] != INPUT_REQUEST_HEADER return
    let result: Word = inputRead(&mut response[2] as *mut UByte)
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
export { inputHandle, inputMain }

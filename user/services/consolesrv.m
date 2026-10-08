// The text-console server as an ordinary program: the checked protocol of the
// page-bounded image in src/kernel/bootstrap.asm, started through a runtime start
// record. Its creator grants DEVICE_UART_TX (SYS_TASK_DEVICES) and the receive
// handle; it holds nothing else, and a request is validated whole before any byte
// reaches the UART.
import { RuntimeStart } from "../../src/task/runtime_start.m"
import { START_BLOCK_VA, RUNTIME_START_BYTES, RUNTIME_START_MAGIC, RUNTIME_START_VERSION,
    CONSOLE_VERSION, CONSOLE_WRITE, CONSOLE_HEADER_BYTES, CONSOLE_TEXT_MAX,
    CONSOLE_RESPONSE_HEADER, CONSOLE_RESPONSE_BYTES, ERRNO_EINVAL, ERRNO_EMSGSIZE } from "../../src/arch/wrm081632/defs.m"
import { debugPutChar, accept, reply, exit, AcceptResult } from "../syscalls.m"

let mut consoleRequest: UByte[32]
let mut consoleResponse: UWord[3]

// Printable ASCII plus tab, line feed and carriage return.
let consoleTextValid(count: UWord): Bool {
    for i: UWord in 0..count {
        let c: UWord = consoleRequest[CONSOLE_HEADER_BYTES + i] as UWord
        if c == 9 || c == 10 || c == 13 continue
        if c < 32 || c >= 127 return false
    }
    return true
}

let consoleServerMain(start: *RuntimeStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != RUNTIME_START_BYTES ||
        start.magic != RUNTIME_START_MAGIC || start.version != RUNTIME_START_VERSION || start.endpoint == 0) exit(1)
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(start.endpoint, &mut consoleRequest[0] as *mut UByte, 32, &mut accepted)
        if size < 0 exit(2)
        let length: UWord = size as UWord
        let mut status: Word = 0
        let mut written: UWord = 0
        if length < CONSOLE_HEADER_BYTES || (consoleRequest[0] as UWord) != CONSOLE_VERSION ||
            (consoleRequest[1] as UWord) != CONSOLE_WRITE || consoleRequest[3] != 0 status = -ERRNO_EINVAL
        else {
            let count: UWord = consoleRequest[2] as UWord
            if count > CONSOLE_TEXT_MAX status = -ERRNO_EMSGSIZE
            else if length != count + CONSOLE_HEADER_BYTES || !consoleTextValid(count) status = -ERRNO_EINVAL
            else {
                while written < count {
                    let put: Word = debugPutChar(consoleRequest[CONSOLE_HEADER_BYTES + written] as UWord)
                    if put < 0 {
                        status = put
                        break
                    }
                    written += 1
                }
            }
        }
        consoleResponse[0] = CONSOLE_RESPONSE_HEADER
        consoleResponse[1] = status as UWord
        consoleResponse[2] = written
        let sent: Word = reply(accepted.replyToken, &consoleResponse[0] as *UByte, CONSOLE_RESPONSE_BYTES)
        if sent > 32 exit(3)
    }
}
export { consoleServerMain }

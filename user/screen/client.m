import { call, callTimed } from "../syscalls.m"
import { SCREEN_REQUEST_HEADER, SCREEN_RESPONSE_HEADER, ERRNO_EMSGSIZE,
    ERRNO_EFAULT, ERRNO_EPROTO } from "../../src/arch/wrm081632/defs.m"

type ScreenRequest {
    header: UWord,
    text: UByte[28],
}
type ScreenResponse {
    header: UWord,
    status: Word,
    count: UWord,
}

// seconds == 0 waits without a deadline (fixed boot profile). A supervised client
// passes 1..60 so a stalled Screen produces -ETIMEDOUT instead of a stuck caller.
let screenWriteFor(endpoint: UWord, text: *UByte, bytes: UWord, seconds: UWord): Word {
    if bytes > 28 return -ERRNO_EMSGSIZE
    if text == null && bytes != 0 return -ERRNO_EFAULT
    let mut request: ScreenRequest
    let mut response: ScreenResponse
    request.header = SCREEN_REQUEST_HEADER | (bytes << 16)
    for i: UWord in 0..bytes request.text[i] = text[i]
    let mut size: Word = 0
    if seconds == 0 size = call(endpoint, &request as *UByte, bytes + 4, &mut response as *mut UByte, 12)
    else size = callTimed(endpoint, &request as *UByte, bytes + 4, &mut response as *mut UByte, 12, seconds)
    if size < 0 return size
    if size != 12 || response.header != SCREEN_RESPONSE_HEADER || response.status > 0 ||
        response.count > bytes || (response.status == 0 && response.count != bytes) return -ERRNO_EPROTO
    if response.status != 0 return response.status
    return response.count as Word
}

let screenWrite(endpoint: UWord, text: *UByte, bytes: UWord): Word {
    return screenWriteFor(endpoint, text, bytes, 0)
}

export { screenWrite, screenWriteFor }

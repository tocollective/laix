import { call } from "../syscalls.m"
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

let screenWrite(endpoint: UWord, text: *UByte, bytes: UWord): Word {
    if bytes > 28 return -ERRNO_EMSGSIZE
    if text == null && bytes != 0 return -ERRNO_EFAULT
    let mut request: ScreenRequest
    let mut response: ScreenResponse
    request.header = SCREEN_REQUEST_HEADER | (bytes << 16)
    for i: UWord in 0..bytes request.text[i] = text[i]
    let size: Word = call(endpoint, &request as *UByte, bytes + 4, &mut response as *mut UByte, 12)
    if size < 0 return size
    if size != 12 || response.header != SCREEN_RESPONSE_HEADER || response.status > 0 ||
        response.count > bytes || (response.status == 0 && response.count != bytes) return -ERRNO_EPROTO
    if response.status != 0 return response.status
    return response.count as Word
}

export { screenWrite }

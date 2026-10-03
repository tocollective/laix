// Public bounded clients. Responses are checked before publishing payload.
import { INPUT_REQUEST_HEADER, INPUT_RESPONSE_HEADER, FILE_REQUEST_HEADER,
    FILE_STAT_HEADER, FILE_RESPONSE_HEADER, FILE_FONT_ID, DATA_GENERATION,
    ERRNO_EINVAL, ERRNO_EPROTO } from "../../src/arch/wrm081632/defs.m"
import { call } from "../syscalls.m"
let mut clientRequest: UWord[5]
let mut clientResponse: UWord[8]

let clientStatus(size: Word, header: UWord): Word {
    if size < 0 return size
    if size != 32 || clientResponse[0] != header return -ERRNO_EPROTO
    let status: Word = clientResponse[1] as Word
    if status > 0 || (status < 0 && clientResponse[3] != 0) return -ERRNO_EPROTO
    return status
}

// destination holds four HID words; flags receives the sticky overflow bit.
let inputEvents(handle: UWord, destination: *mut UWord, flags: *mut UWord): Word {
    if destination == null || flags == null return -ERRNO_EINVAL
    clientRequest[0] = INPUT_REQUEST_HEADER
    let size: Word = call(handle, &clientRequest[0] as *UByte, 4, &mut clientResponse[0] as *mut UByte, 32)
    // INPUT count is word 2; word 3 is flags, unlike file responses.
    if size < 0 return size
    if size != 32 || clientResponse[0] != INPUT_RESPONSE_HEADER return -ERRNO_EPROTO
    let status: Word = clientResponse[1] as Word
    if status > 0 || clientResponse[2] > 4 || clientResponse[3] & ~(1 as UWord) != 0 ||
        (status < 0 && (clientResponse[2] != 0 || clientResponse[3] != 0)) return -ERRNO_EPROTO
    if status < 0 return status
    for i: UWord in 0..4 {
        if i < clientResponse[2] {
            if clientResponse[4 + i] & 0x7FFF0000 != 0 return -ERRNO_EPROTO
        } else if clientResponse[4 + i] != 0 return -ERRNO_EPROTO
    }
    for i: UWord in 0..4 destination[i] = clientResponse[4 + i]
    flags[0] = clientResponse[3]
    return clientResponse[2] as Word
}

let fileQuery(handle: UWord, header: UWord, offset: UWord, bytes: UWord): Word {
    clientRequest[0] = header
    clientRequest[1] = DATA_GENERATION
    clientRequest[2] = FILE_FONT_ID
    clientRequest[3] = offset
    clientRequest[4] = bytes
    let mut requestBytes: UWord = 20
    if header == FILE_STAT_HEADER requestBytes = 16
    let size: Word = call(handle, &clientRequest[0] as *UByte, requestBytes, &mut clientResponse[0] as *mut UByte, 32)
    if size < 0 return size
    if size != 32 || clientResponse[2] != DATA_GENERATION return -ERRNO_EPROTO
    let status: Word = clientStatus(size, FILE_RESPONSE_HEADER)
    if status != 0 return status
    return 0
}

let fileSize(handle: UWord): Word {
    let status: Word = fileQuery(handle, FILE_STAT_HEADER, 0, 0)
    if status != 0 return status
    if clientResponse[3] > 0x7FFFFFFF return -ERRNO_EPROTO
    return clientResponse[3] as Word
}

let fileRead(handle: UWord, offset: UWord, destination: *mut UByte, bytes: UWord): Word {
    if destination == null || bytes == 0 || bytes > 16 return -ERRNO_EINVAL
    let status: Word = fileQuery(handle, FILE_REQUEST_HEADER, offset, bytes)
    if status != 0 return status
    let count: UWord = clientResponse[3]
    if count > bytes return -ERRNO_EPROTO
    let source: *UByte = &clientResponse[4] as *UByte
    for i: UWord in 0..count destination[i] = source[i]
    return count as Word
}
export { inputEvents, fileSize, fileRead }

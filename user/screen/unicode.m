// Bounded, strict UTF-8 decoding for version-2 screen messages. No terminator,
// cross-request state, replacement of malformed input or speculative reads.
import { ERRNO_EINVAL, UNICODE_MAX, UNICODE_SURROGATE_MIN,
    UNICODE_SURROGATE_MAX } from "../../src/arch/wrm081632/defs.m"

type Decoded {
    code: UWord,
    next: UWord,
}

let decodeNext(text: *UByte, bytes: UWord, offset: UWord, result: *mut Decoded): Word {
    if text == null || result == null || offset >= bytes return -ERRNO_EINVAL
    let first: UWord = text[offset] as UWord
    let mut code: UWord = first
    let mut extra: UWord = 0
    let mut minimum: UWord = 0
    if first >= 0xC2 && first <= 0xDF {
        extra = 1
        code = first & 31
        minimum = 0x80
    } else if first >= 0xE0 && first <= 0xEF {
        extra = 2
        code = first & 15
        minimum = 0x800
    } else if first >= 0xF0 && first <= 0xF4 {
        extra = 3
        code = first & 7
        minimum = 0x10000
    } else if first >= 0x80 return -ERRNO_EINVAL
    if extra > bytes - offset - 1 return -ERRNO_EINVAL
    for i: UWord in 0..extra {
        let next: UWord = text[offset + i + 1] as UWord
        if next < 0x80 || next > 0xBF return -ERRNO_EINVAL
        code = code << 6 | (next & 63)
    }
    if code < minimum || code > UNICODE_MAX ||
        (code >= UNICODE_SURROGATE_MIN && code <= UNICODE_SURROGATE_MAX) ||
        (code < 32 && code != 9 && code != 10 && code != 13) ||
        (code >= 0x7F && code <= 0x9F) return -ERRNO_EINVAL
    result.code = code
    result.next = offset + extra + 1
    return 0
}

let validateText(text: *UByte, bytes: UWord): Word {
    let mut decoded: Decoded
    let mut offset: UWord = 0
    while offset < bytes {
        let status: Word = decodeNext(text, bytes, offset, &mut decoded)
        if status != 0 return status
        offset = decoded.next
    }
    return 0
}

export { Decoded, decodeNext, validateText }

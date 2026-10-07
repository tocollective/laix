import { ServiceStart, serviceStartValid } from "../../src/task/service_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_SERVER,
    SCREEN_RESPONSE_HEADER, ERRNO_EINVAL, ERRNO_EMSGSIZE,
    ERRNO_EIO, SCREEN_WIDTH, SCREEN_HEIGHT, GLYPH_HEIGHT } from "../../src/arch/wrm081632/defs.m"
import { AcceptResult, accept, reply, exit } from "../syscalls.m"
import { font, loadFont, glyphIndex } from "font.m"
import { Decoded, decodeNext, validateText } from "unicode.m"
import { cacheInit, cacheGlyph } from "cache.m"
import { videoInit, videoGlyph, videoScroll, videoFrame } from "video.m"

let mut screenRequest: UByte[32]
let mut screenResponse: UWord[3]
let mut column: UWord
let mut row: UWord
let mut screenFailed: Bool

let screenNewline(): Void {
    column = 0
    if row + GLYPH_HEIGHT < SCREEN_HEIGHT row += GLYPH_HEIGHT
    else videoScroll()
}

let screenCode(code: UWord): Word {
    if code == 10 {
        screenNewline()
        return 0
    }
    if code == 13 {
        column = 0
        return 0
    }
    if code == 9 {
        column = ((column / 64) + 1) * 64
        if column >= SCREEN_WIDTH screenNewline()
        return 0
    }
    let mut glyph: UWord = glyphIndex(code)
    if glyph == font.count glyph = font.fallback
    let advance: UWord = font.index[glyph].advance
    let slot: Word = cacheGlyph(glyph)
    if slot < 0 return slot
    if column > SCREEN_WIDTH - advance screenNewline()
    let status: Word = videoGlyph(column, row, slot as UWord, advance)
    if status != 0 return status
    column += advance
    if column == SCREEN_WIDTH screenNewline()
    return 0
}

let screenHandle(request: *UByte, size: UWord, response: *mut UWord): Void {
    response[0] = SCREEN_RESPONSE_HEADER
    response[1] = (-ERRNO_EINVAL) as UWord
    response[2] = 0
    if size < 4 || request[0] != 2 || request[1] != 1 || request[3] != 0 return
    let bytes: UWord = request[2] as UWord
    if bytes > 28 {
        response[1] = (-ERRNO_EMSGSIZE) as UWord
        return
    }
    if size != bytes + 4 || validateText(&request[4], bytes) != 0 return
    if screenFailed {
        response[1] = (-ERRNO_EIO) as UWord
        return
    }
    let mut decoded: Decoded
    let mut offset: UWord = 0
    while offset < bytes {
        let mut status: Word = decodeNext(&request[4], bytes, offset, &mut decoded)
        if status == 0 status = screenCode(decoded.code)
        if status != 0 {
            screenFailed = true
            response[1] = status as UWord
            return
        }
        offset = decoded.next
        response[2] = offset
    }
    response[1] = 0
    if bytes != 0 {
        let status: Word = videoFrame()
        if status != 0 {
            screenFailed = true
            response[1] = status as UWord
        }
    }
}

// Shared by the fixed bootstrap service and the supervised one; only the startup
// record that supplies these five values differs.
let screenServe(endpoint: UWord, bitmap: UWord, irq: UWord, fontIndex: UWord, fontBytes: UWord): Void {
    if !loadFont(fontIndex as *UByte, fontBytes) || videoInit(irq) != 0 exit(1)
    cacheInit(bitmap)
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(endpoint, &mut screenRequest[0], 32, &mut accepted)
        if size < 0 exit(1)
        screenHandle(&screenRequest[0], size as UWord, &mut screenResponse[0])
        let sent: Word = reply(accepted.replyToken, &screenResponse[0] as *UByte, 12)
        if sent > 12 exit(1)
    }
}

let screenMain(start: *ServiceStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != SERVICE_START_BYTES ||
        !serviceStartValid(start) || start.role != START_ROLE_SERVER) exit(1)
    screenServe(start.endpoint, start.bitmapEndpoint, start.irq, start.fontIndex, start.fontBytes)
}

export { screenMain, screenServe, screenHandle }

// User-owned FIFO glyph cache. Disk authority belongs to the bitmap endpoint,
// never this module. A slot is published only after both validated halves.
import { FONT_GENERATION, FONT_REQUEST_HEADER, FONT_VALIDATE_HEADER,
    FONT_RESPONSE_HEADER, ERRNO_EPROTO } from "../../src/arch/wrm081632/defs.m"
import { call } from "../syscalls.m"
import { videoUpload } from "video.m"

let CACHE_GLYPHS: UWord = 256
let mut cachedGlyphs: UWord[CACHE_GLYPHS]
let mut bitmapEndpoint: UWord
let mut nextSlot: UWord
let mut bitmapRequest: UWord[4]
let mut bitmapResponse: UWord[8]
let mut stagedGlyph: UWord[8]

let cacheInit(endpoint: UWord): Void {
    bitmapEndpoint = endpoint
    nextSlot = 0
    for i: UWord in 0..CACHE_GLYPHS cachedGlyphs[i] = 0xFFFFFFFF
}

let bitmapRead(glyph: UWord, chunk: UWord, validate: Bool): Word {
    bitmapRequest[0] = FONT_REQUEST_HEADER
    if validate bitmapRequest[0] = FONT_VALIDATE_HEADER
    bitmapRequest[1] = FONT_GENERATION
    bitmapRequest[2] = glyph
    bitmapRequest[3] = chunk
    let size: Word = call(bitmapEndpoint, &bitmapRequest[0] as *UByte, 16,
        &mut bitmapResponse[0] as *mut UByte, 32)
    if size < 0 return size
    if size != 32 || bitmapResponse[0] != FONT_RESPONSE_HEADER ||
        bitmapResponse[2] != FONT_GENERATION return -ERRNO_EPROTO
    let status: Word = bitmapResponse[1] as Word
    if status > 0 return -ERRNO_EPROTO
    let mut count: UWord = 16
    if status != 0 || validate count = 0
    if bitmapResponse[3] != count return -ERRNO_EPROTO
    if count == 0 {
        for i: UWord in 4..8 {
            if bitmapResponse[i] != 0 return -ERRNO_EPROTO
        }
    }
    return status
}

let cacheGlyph(glyph: UWord): Word {
    for i: UWord in 0..CACHE_GLYPHS {
        if cachedGlyphs[i] != glyph continue
        let status: Word = bitmapRead(0, 0, true)
        if status != 0 return status
        return i as Word
    }
    for chunk: UWord in 0..2 {
        let status: Word = bitmapRead(glyph, chunk, false)
        if status != 0 return status
        for i: UWord in 0..4 stagedGlyph[chunk * 4 + i] = bitmapResponse[4 + i]
    }
    let slot: UWord = nextSlot
    let status: Word = videoUpload(slot, &stagedGlyph[0] as *UByte)
    if status != 0 return status
    cachedGlyphs[slot] = glyph
    nextSlot = (slot + 1) % CACHE_GLYPHS
    return slot as Word
}

export { cacheInit, cacheGlyph }

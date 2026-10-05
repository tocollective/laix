// CPU-only rendering into the exclusive VRAM grant. The MMIO alias is RO;
// no command register, physical-memory DMA or supervisor operation is used.
import { SCREEN_VRAM_VA, SCREEN_VIDEO_VA, SCREEN_VRAM_BYTES, SCREEN_WIDTH,
    SCREEN_HEIGHT, SCREEN_PITCH, GLYPH_HEIGHT, GLYPH_BYTES, ERRNO_EIO,
    ERRNO_EINVAL, ERRNO_EBUSY } from "../../src/arch/wrm081632/defs.m"
import { screenControl, irqWait, irqComplete } from "../syscalls.m"

let FRAME_BYTES: UWord = SCREEN_WIDTH * SCREEN_HEIGHT
let videoRegisters: *volatile UWord = SCREEN_VIDEO_VA as *volatile UWord
let vram: *volatile mut UByte = SCREEN_VRAM_VA as *volatile mut UByte
let mut videoIrq: UWord

let videoClear(): Void {
    let words: *volatile mut UWord = SCREEN_VRAM_VA as *volatile mut UWord
    for i: UWord in 0..(FRAME_BYTES / 4) words[i] = 0
    fence()
}

let videoInit(irq: UWord): Word {
    let mut status: Word = screenControl(4, 0)
    if status != 0 return status
    status = screenControl(32, 0)
    if status != 0 return status
    status = screenControl(8, 0x21) // user policy: 640x480, 8bpp
    if status != 0 return status
    status = screenControl(40, 0)
    if status != 0 return status
    status = screenControl(44, 0)
    if status != 0 return status
    status = screenControl(44, 0xFFFFFF)
    if status != 0 return status
    status = screenControl(128, 0)
    if status != 0 return status
    if videoRegisters[3] != SCREEN_WIDTH || videoRegisters[4] != SCREEN_HEIGHT ||
        videoRegisters[5] != 8 || videoRegisters[6] != SCREEN_PITCH ||
        videoRegisters[7] < SCREEN_VRAM_BYTES return -ERRNO_EIO
    videoIrq = irq
    videoClear()
    status = videoRearm()
    if status != 0 return status
    return screenControl(4, 5)
}

// An asserted shared cause or a VBLANK racing acknowledgement keeps the IRQ
// masked. Service both causes again before rearming; never wait on that mask.
let videoRearm(): Word {
    for attempt: UWord in 0..4 {
        let mut status: Word = screenControl(0, 10)
        if status != 0 return status
        status = irqComplete(videoIrq)
        if status != -ERRNO_EBUSY return status
    }
    return -ERRNO_EBUSY
}
let videoFrame(): Word {
    let status: Word = irqWait(videoIrq, 5)
    if status != 0 return status
    return videoRearm()
}

let videoScroll(): Void {
    let rows: UWord = FRAME_BYTES - SCREEN_PITCH * GLYPH_HEIGHT
    for i: UWord in 0..rows vram[i] = vram[i + SCREEN_PITCH * GLYPH_HEIGHT]
    for i: UWord in rows..FRAME_BYTES vram[i] = 0
    fence()
}

let videoUpload(slot: UWord, bitmap: *UByte): Word {
    if bitmap == null || slot >= (SCREEN_VRAM_BYTES - FRAME_BYTES) / GLYPH_BYTES return -ERRNO_EINVAL
    let offset: UWord = FRAME_BYTES + slot * GLYPH_BYTES
    for i: UWord in 0..GLYPH_BYTES vram[offset + i] = bitmap[i]
    fence()
    return 0
}

let videoGlyph(x: UWord, y: UWord, slot: UWord, advance: UWord): Word {
    if ((advance != 8 && advance != 16) || x > SCREEN_WIDTH - advance ||
        y > SCREEN_HEIGHT - GLYPH_HEIGHT ||
        slot >= (SCREEN_VRAM_BYTES - FRAME_BYTES) / GLYPH_BYTES) return -ERRNO_EINVAL
    let base: UWord = FRAME_BYTES + slot * GLYPH_BYTES
    for row: UWord in 0..GLYPH_HEIGHT {
        for column: UWord in 0..advance {
            let bits: UWord = vram[base + row * 2 + column / 8] as UWord
            vram[(y + row) * SCREEN_PITCH + x + column] = ((bits >> (7 - column % 8)) & 1) as UByte
        }
    }
    fence()
    return 0
}

export { videoRearm, videoInit, videoFrame, videoScroll, videoUpload, videoGlyph }

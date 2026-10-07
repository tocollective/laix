// CPU-only crash fixture: the production Screen loop plus two hooks. A one-glyph
// write of '!' renders into VRAM and dies before the frame wait and the reply, so
// the client's call holds an accepted reply right and VBLANK is left asserted.
// Independently, generations 1-3 fault on their third request, so an unmodified
// production client and supervisor meet real failures. Neither hook is production.
import { RecoveryStart } from "../../../src/task/recovery_start.m"
import { screenHandle } from "../../../user/screen/server.m"
import { font, loadFont, glyphIndex } from "../../../user/screen/font.m"
import { cacheInit, cacheGlyph } from "../../../user/screen/cache.m"
import { videoInit, videoGlyph } from "../../../user/screen/video.m"
import { accept, reply, exit, AcceptResult } from "../../../user/syscalls.m"
import { RECOVERY_START_BYTES, RUNTIME_START_MAGIC } from "../../../src/arch/wrm081632/defs.m"
extern let recoveryFault(): Void
let mut request: UByte[32]
let mut response: UWord[3]
let mut served: UWord
let fixtureScreenMain(start: *RecoveryStart, bytes: UWord): Void {
    if bytes != RECOVERY_START_BYTES || start.magic != RUNTIME_START_MAGIC ||
        start.generation == 0 || start.reference == 0 || start.dependency == 0 ||
        start.irq == 0 || start.blob == 0 || start.blobBytes == 0 exit(1)
    if !loadFont(start.blob as *UByte, start.blobBytes) || videoInit(start.irq) != 0 exit(1)
    cacheInit(start.dependency)
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(start.endpoint, &mut request[0], 32, &mut accepted)
        if size < 0 exit(2)
        served += 1
        if served == 3 && start.generation < 4 recoveryFault()
        if size == 5 && request[4] == '!' as UByte && start.generation < 5 {
            let glyph: UWord = glyphIndex('!' as UWord)
            let slot: Word = cacheGlyph(glyph)
            if slot < 0 exit(3)
            if videoGlyph(0, 0, slot as UWord, font.index[glyph].advance) != 0 exit(4)
            recoveryFault()
        }
        screenHandle(&request[0], size as UWord, &mut response[0])
        let sent: Word = reply(accepted.replyToken, &response[0] as *UByte, 12)
        if sent > 12 exit(5)
    }
}
export { fixtureScreenMain }

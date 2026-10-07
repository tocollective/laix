// Acceptance child for the Files-backed loader. It exists only as bytes on the
// storage volume: no kernel catalog row names it. Its three segments are all
// used: .text runs, .rodata supplies a constant, .data is written and .bss must
// arrive zeroed. The exit code (5 + argument) * 9 proves each of them; argument
// 0xDEAD takes the unaligned-read fault path like the catalog's approved image.
import { RuntimeStart } from "../../../src/task/runtime_start.m"
import { START_BLOCK_VA, RUNTIME_START_BYTES, RUNTIME_START_MAGIC, RUNTIME_START_VERSION } from "../../../src/arch/wrm081632/defs.m"
import { exit } from "../../../user/syscalls.m"
let HELLO_FACTOR: UWord = 9
let mut helloSeed: UWord = 5
let mut helloScratch: UWord[16]

let helloMain(start: *RuntimeStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != RUNTIME_START_BYTES ||
        start.magic != RUNTIME_START_MAGIC || start.version != RUNTIME_START_VERSION) exit(1)
    if start.argument == 0xDEAD {
        let bad: *UWord = 1 as *UWord
        helloScratch[0] = bad[0]
    }
    helloSeed += start.argument
    // .bss must be zero when the task starts; any residue changes the code.
    let mut residue: UWord = 0
    for i: UWord in 0..16 residue += helloScratch[i]
    exit((helloSeed * HELLO_FACTOR + residue) as Word)
}
export { helloMain }

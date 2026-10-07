// Counts from 1 to its argument (at most 50), one number per line.
import { RuntimeStart } from "../../src/task/runtime_start.m"
import { START_BLOCK_VA, RUNTIME_START_BYTES, RUNTIME_START_MAGIC, RUNTIME_START_VERSION } from "../../src/arch/wrm081632/defs.m"
import { exit } from "../syscalls.m"
import { textStart, textFlush, textLine, textNumber } from "../text.m"

let binCountMain(start: *RuntimeStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != RUNTIME_START_BYTES ||
        start.magic != RUNTIME_START_MAGIC || start.version != RUNTIME_START_VERSION || start.endpoint == 0) exit(100)
    textStart(start.endpoint)
    let mut limit: UWord = start.argument
    if limit > 50 limit = 50
    for n: UWord in 1..limit + 1 {
        textNumber(n, 3)
        textLine()
    }
    textFlush()
    exit(limit as Word)
}
export { binCountMain }

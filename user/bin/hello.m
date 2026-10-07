// A program stored on the volume: greets through the console endpoint it was
// started with and exits with its argument.
import { RuntimeStart } from "../../src/task/runtime_start.m"
import { START_BLOCK_VA, RUNTIME_START_BYTES, RUNTIME_START_MAGIC, RUNTIME_START_VERSION } from "../../src/arch/wrm081632/defs.m"
import { exit } from "../syscalls.m"
import { textStart, textFlush, textString, textLine, textNumber } from "../text.m"

let binHelloMain(start: *RuntimeStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != RUNTIME_START_BYTES ||
        start.magic != RUNTIME_START_MAGIC || start.version != RUNTIME_START_VERSION || start.endpoint == 0) exit(100)
    textStart(start.endpoint)
    textString("Hello from a loaded program, argument ")
    textNumber(start.argument, 0)
    textLine()
    textFlush()
    exit(start.argument as Word)
}
export { binHelloMain }

// The boot banner as an ordinary program: writes the line through the console
// endpoint it was started with and exits. The console session runs it.
import { RuntimeStart } from "../../src/task/runtime_start.m"
import { START_BLOCK_VA, RUNTIME_START_BYTES, RUNTIME_START_MAGIC, RUNTIME_START_VERSION } from "../../src/arch/wrm081632/defs.m"
import { exit } from "../syscalls.m"
import { textStart, textFlush, textString } from "../text.m"

let bannerMain(start: *RuntimeStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != RUNTIME_START_BYTES ||
        start.magic != RUNTIME_START_MAGIC || start.version != RUNTIME_START_VERSION || start.endpoint == 0) exit(100)
    textStart(start.endpoint)
    textString("LA/IX microkernel v1.0.0\n")
    textFlush()
    exit(0)
}
export { bannerMain }

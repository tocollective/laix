// Endpoint handles handed to a boot-constructed task beyond its single start
// endpoint. The kernel's boot policy writes them to the first words of the task's
// data page before the task runs: magic, count, then the handles. The page is the
// task's own and writable; the list is a convention for the first words only and
// confers nothing the handles' own rights do not.
import { RuntimeStart } from "../src/task/runtime_start.m"
import { START_HANDLES_MAGIC, START_HANDLES_MAX } from "../src/arch/wrm081632/defs.m"

// The index-th handle, or zero when the list is absent or shorter.
let startHandle(start: *RuntimeStart, index: UWord): UWord {
    let list: *UWord = start.data as *UWord
    if list[0] != START_HANDLES_MAGIC || list[1] > START_HANDLES_MAX || index >= list[1] return 0
    return list[2 + index]
}
export { startHandle }

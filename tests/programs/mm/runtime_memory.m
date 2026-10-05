// Separate CPU fixture; no ordinary boot service policy changes.
import { kernelInit, kernelBootInfo } from "../../../src/kernel/boot.m"
import { taskCreateProgram } from "../../../src/task/program.m"
import { taskGet, Task, taskPublish, taskStart } from "../../../src/task/task.m"
import { taskInstallRuntimeStart, taskControlBootstrapSelf } from "../../../src/task/control.m"
import { endpointBootstrap, handleCopy } from "../../../src/ipc/objects.m"
import { RIGHT_SEND, RIGHT_RECEIVE } from "../../../src/arch/wrm081632/defs.m"
import { panic, setPanicStage } from "../../../src/kernel/panic.m"
extern let memoryImageStart: UByte
extern let memoryImageEnd: UByte
let main(): Word {
    kernelInit()
    setPanicStage("runtime-memory-prepare")
    let first: UWord = taskCreateProgram(&memoryImageStart as UWord, &memoryImageEnd as UWord)
    let second: UWord = taskCreateProgram(&memoryImageStart as UWord, &memoryImageEnd as UWord)
    if first == 0 || second == 0 panic("memory fixture image allocation failed", null)
    let app: *mut Task = taskGet(first)
    let peer: *mut Task = taskGet(second)
    let endpoint: Word = endpointBootstrap(&mut app.handles, first)
    if endpoint <= 0 panic("memory fixture endpoint failed", null)
    let copied: Word = handleCopy(&mut app.handles, endpoint as UWord, &mut peer.handles,
        first, second, RIGHT_SEND | RIGHT_RECEIVE)
    if copied <= 0 panic("memory fixture handle copy failed", null)
    if !taskInstallRuntimeStart(first, endpoint as UWord, RIGHT_SEND | RIGHT_RECEIVE, 0) ||
        !taskInstallRuntimeStart(second, copied as UWord, RIGHT_SEND | RIGHT_RECEIVE, 1) ||
        !taskControlBootstrapSelf(first) panic("memory fixture startup failed", null)
    app.createImages = 1
    if !taskPublish(first) || !taskPublish(second) panic("memory fixture publication failed", null)
    setPanicStage("runtime-memory-user")
    taskStart(kernelBootInfo.clock)
    return 1
}

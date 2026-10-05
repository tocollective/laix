// Acceptance-only boot policy. File/Disk peers remain ordinary user services.
import { kernelInit, kernelBootInfo } from "../../../src/kernel/boot.m"
import { bootstrapSimpleInit } from "../../../src/kernel/simple_bootstrap.m"
import { Task, taskGet, taskInstallServiceStart, taskPublish, taskStart } from "../../../src/task/task.m"
import { taskCreateProgram } from "../../../src/task/program.m"
import { ServiceStart } from "../../../src/task/service_start.m"
import { handleCopy } from "../../../src/ipc/objects.m"
import { RIGHT_SEND } from "../../../src/arch/wrm081632/defs.m"
import { panic } from "../../../src/kernel/panic.m"
extern let simpleApplicationImage: UByte
extern let simpleApplicationImageEnd: UByte
let main(): Word {
    kernelInit()
    if !bootstrapSimpleInit() panic("Input stress bootstrap failed", null)
    let source: *mut Task = taskGet(4)
    for i: UWord in 0..4 {
        let id: UWord = taskCreateProgram(&simpleApplicationImage as UWord, &simpleApplicationImageEnd as UWord)
        if id == 0 panic("Input client creation failed", null)
        let child: *mut Task = taskGet(id)
        let mut block: ServiceStart = (source.bootPage as *ServiceStart)[0]
        let send: Word = handleCopy(&mut source.handles, block.endpoint, &mut child.handles, 4, id, RIGHT_SEND)
        let input: Word = handleCopy(&mut source.handles, block.bitmapEndpoint, &mut child.handles, 4, id, RIGHT_SEND)
        if send < 0 || input < 0 panic("Input client grants failed", null)
        block.taskId = id
        block.endpoint = send as UWord
        block.bitmapEndpoint = input as UWord
        if !taskInstallServiceStart(id, &block, 3) || !taskPublish(id) panic("Input client publication failed", null)
    }
    taskStart(kernelBootInfo.clock)
    return 1
}

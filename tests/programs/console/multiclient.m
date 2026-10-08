// Trusted acceptance bootstrap only; excluded from ordinary image profiles.
import { kernelInit, kernelBootInfo } from "../../../src/kernel/boot.m"
import { bootstrapInit } from "../boot/uart_bootstrap.m"
import { bootstrapScreenInit } from "../boot/service_bootstrap.m"
import { taskGet, Task, taskInstallStart, taskInstallServiceStart, taskPublish, taskStart } from "../../../src/task/task.m"
import { taskCreateProgram } from "../../../src/task/program.m"
import { TaskStart } from "../../../src/task/start.m"
import { ServiceStart } from "../../../src/task/service_start.m"
import { handleCopy } from "../../../src/ipc/objects.m"
import { RIGHT_SEND } from "../../../src/arch/wrm081632/defs.m"
import { panic } from "../../../src/kernel/panic.m"
extern let stressImage: UByte
extern let stressImageEnd: UByte
let acceptanceClients(screen: Bool): Bool {
    let mut original: UWord = 2
    if screen original = 3
    let source: *mut Task = taskGet(original)
    let token: UWord = (source.bootPage as *TaskStart).endpoint
    for i: UWord in 0..4 {
        let id: UWord = taskCreateProgram(&stressImage as UWord, &stressImageEnd as UWord)
        if id == 0 return false
        let child: *mut Task = taskGet(id)
        let send: Word = handleCopy(&mut source.handles, token, &mut child.handles, original, id, RIGHT_SEND)
        if send < 0 return false
        if screen {
            let mut block: ServiceStart = (source.bootPage as *ServiceStart)[0]
            block.taskId = id
            block.endpoint = send as UWord
            if !taskInstallServiceStart(id, &block, 3) return false
        } else {
            let mut block: TaskStart = (source.bootPage as *TaskStart)[0]
            block.taskId = id
            block.endpoint = send as UWord
            if !taskInstallStart(id, &block) return false
        }
        if !taskPublish(id) return false
    }
    return true
}
let uartStressMain(): Word {
    kernelInit()
    // Two entry functions let one source define both artifact profiles.
    if !bootstrapInit() || !acceptanceClients(false) panic("UART stress bootstrap failed", null)
    taskStart(kernelBootInfo.clock)
    return 1
}
let screenStressMain(): Word {
    kernelInit()
    if !bootstrapScreenInit() || !acceptanceClients(true) panic("Screen stress bootstrap failed", null)
    taskStart(kernelBootInfo.clock)
    return 1
}
export { uartStressMain, screenStressMain }

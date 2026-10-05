// Dedicated runtime capability CPU image; boot installs only the control channel.
import { kernelInit, kernelBootInfo } from "../../../src/kernel/boot.m"
import { taskCreateProgram } from "../../../src/task/program.m"
import { taskGet, Task, taskPublish, taskStart } from "../../../src/task/task.m"
import { taskInstallRuntimeStart, taskControlBootstrapSelf } from "../../../src/task/control.m"
import { endpointBootstrap, handleCopy, endpointFactoryBootstrap,
    ENDPOINT_FACTORY_QUOTA } from "../../../src/ipc/objects.m"
import { RIGHT_SEND, RIGHT_RECEIVE, DEVICE_UART_TX, DEVICE_INPUT } from "../../../src/arch/wrm081632/defs.m"
import { panic, setPanicStage } from "../../../src/kernel/panic.m"
extern let objectsImageStart: UByte
extern let objectsImageEnd: UByte
let main(): Word {
    kernelInit()
    setPanicStage("runtime-objects-prepare")
    let first: UWord = taskCreateProgram(&objectsImageStart as UWord, &objectsImageEnd as UWord)
    let second: UWord = taskCreateProgram(&objectsImageStart as UWord, &objectsImageEnd as UWord)
    if first == 0 || second == 0 panic("object fixture image allocation failed", null)
    let app: *mut Task = taskGet(first)
    let peer: *mut Task = taskGet(second)
    let endpoint: Word = endpointBootstrap(&mut app.handles, first)
    let copied: Word = handleCopy(&mut app.handles, endpoint as UWord, &mut peer.handles,
        first, second, RIGHT_SEND | RIGHT_RECEIVE)
    if endpoint <= 0 || copied <= 0 ||
        !taskInstallRuntimeStart(first, endpoint as UWord, RIGHT_SEND | RIGHT_RECEIVE, 0) ||
        !taskInstallRuntimeStart(second, copied as UWord, RIGHT_SEND | RIGHT_RECEIVE, 1) ||
        !taskControlBootstrapSelf(first) ||
        !endpointFactoryBootstrap(&mut app.handles, 3, ENDPOINT_FACTORY_QUOTA, true) {
        panic("object fixture startup failed", null)
    }
    app.createImages = 1
    app.deviceFactory = DEVICE_UART_TX | DEVICE_INPUT
    if !taskPublish(first) || !taskPublish(second) panic("object fixture publication failed", null)
    setPanicStage("runtime-objects-user")
    taskStart(kernelBootInfo.clock)
    return 1
}

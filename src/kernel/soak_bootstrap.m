import { endpointFactoryBootstrap, ENDPOINT_FACTORY_QUOTA } from "../ipc/objects.m"
import { DEVICE_UART_TX, DEVICE_INPUT } from "../arch/wrm081632/defs.m"
// Same delegation as supervisor_bootstrap.m, for the soak supervisor code in
// soak_bootstrap.asm.
import { taskInitAvailable, taskCreateImage, taskGet, taskPublish,
    taskDiscardCreated, Task } from "../task/task.m"
import { panic } from "panic.m"
import { taskInstallRuntimeStart, taskControlBootstrapSelf,
    taskReleaseSupervisor } from "../task/control.m"

extern let soakCodeStart: UByte
extern let soakCodeEnd: UByte

let soakBootstrap(): Bool {
    if !taskInitAvailable() return false
    let reference: UWord = taskCreateImage(&soakCodeStart as UWord, &soakCodeEnd as UWord, 0)
    if reference == 0 return false
    if !taskInstallRuntimeStart(reference, 0, 0, 0) || !taskControlBootstrapSelf(reference) {
        taskReleaseSupervisor(reference)
        if !taskDiscardCreated(reference) panic("could not discard soak supervisor", null)
        return false
    }
    let task: *mut Task = taskGet(reference)
    task.createImages = 1
    task.deviceFactory = DEVICE_UART_TX | DEVICE_INPUT
    if !endpointFactoryBootstrap(&mut task.handles, 3, ENDPOINT_FACTORY_QUOTA, true) {
        taskReleaseSupervisor(reference)
        if !taskDiscardCreated(reference) panic("could not discard factory owner", null)
        return false
    }
    if !taskPublish(reference) {
        taskReleaseSupervisor(reference)
        if !taskDiscardCreated(reference) panic("could not discard soak supervisor", null)
        return false
    }
    return true
}

export { soakBootstrap }

import { kernelInit, kernelBootInfo } from "boot.m"
import { panic, setPanicStage } from "panic.m"
import { taskStart } from "../task/task.m"
import { bootstrapScreenInit } from "service_bootstrap.m"

let main(): Word {
    kernelInit()
    setPanicStage("screen-services")
    if !bootstrapScreenInit() {
        panic("could not prepare screen services", null)
        return 1
    }
    setPanicStage("user-task")
    taskStart(kernelBootInfo.clock)
    panic("screen task entry returned", null)
    return 1
}

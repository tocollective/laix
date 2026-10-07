import { kernelInit, kernelBootInfo } from "boot.m"
import { panic, setPanicStage } from "panic.m"
import { taskStart } from "../task/task.m"
import { bootstrapLoaderInit } from "loader_bootstrap.m"
let main(): Word {
    kernelInit()
    setPanicStage("loader-services")
    if !bootstrapLoaderInit() {
        panic("could not prepare loader services", null)
        return 1
    }
    setPanicStage("user-task")
    taskStart(kernelBootInfo.clock)
    panic("loader entry returned", null)
    return 1
}

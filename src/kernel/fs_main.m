import { kernelInit, kernelBootInfo } from "boot.m"
import { panic, setPanicStage } from "panic.m"
import { taskStart } from "../task/task.m"
import { bootstrapFsInit } from "fs_bootstrap.m"
let main(): Word {
    kernelInit()
    setPanicStage("fs-services")
    if !bootstrapFsInit() {
        panic("could not prepare filesystem services", null)
        return 1
    }
    setPanicStage("user-task")
    taskStart(kernelBootInfo.clock)
    panic("filesystem entry returned", null)
    return 1
}

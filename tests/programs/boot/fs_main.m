import { kernelInit, kernelBootInfo } from "../../../src/kernel/boot.m"
import { panic, setPanicStage } from "../../../src/kernel/panic.m"
import { taskStart } from "../../../src/task/task.m"
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

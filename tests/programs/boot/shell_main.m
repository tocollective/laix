import { kernelInit, kernelBootInfo } from "../../../src/kernel/boot.m"
import { panic, setPanicStage } from "../../../src/kernel/panic.m"
import { taskStart } from "../../../src/task/task.m"
import { bootstrapShellInit } from "shell_bootstrap.m"
let main(): Word {
    kernelInit()
    setPanicStage("shell-services")
    if !bootstrapShellInit() {
        panic("could not prepare shell services", null)
        return 1
    }
    setPanicStage("user-task")
    taskStart(kernelBootInfo.clock)
    panic("shell entry returned", null)
    return 1
}

import { kernelInit, kernelBootInfo } from "../../../src/kernel/boot.m"
import { panic, setPanicStage } from "../../../src/kernel/panic.m"
import { taskStart } from "../../../src/task/task.m"
import { bootstrapSimpleInit } from "simple_bootstrap.m"
let main(): Word {
    kernelInit()
    setPanicStage("simple-services")
    if !bootstrapSimpleInit() {
        panic("could not prepare simple services", null)
        return 1
    }
    setPanicStage("user-task")
    taskStart(kernelBootInfo.clock)
    panic("simple task entry returned", null)
    return 1
}

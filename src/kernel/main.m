// LA/IX start.asm clears BSS, installs trapEntry and provides a kernel stack.
// kernelInit enables the stack guard through MMU; IRQs stay disabled until taskStart.
import { kernelInit, kernelBootInfo } from "boot.m"
import { panic, setPanicStage } from "panic.m"
import { taskStart } from "../task/task.m"
import { bootstrapInit } from "bootstrap.m"

let main(): Word {
    kernelInit()
    setPanicStage("task-prepare")
    if !bootstrapInit() {
        panic("could not prepare user tasks", null)
        return 1
    }
    setPanicStage("user-task")
    taskStart(kernelBootInfo.clock)
    panic("user task entry returned", null)
    return 1
}

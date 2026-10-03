// LA/IX start.asm clears BSS, installs trapEntry and provides a kernel stack.
// kernelInit enables the stack guard through MMU; IRQs stay disabled until taskStart.
import { kernelInit, kernelBootInfo } from "boot.m"
import { panic, setPanicStage } from "panic.m"
import { consoleInit, print } from "../console/console.m"
import { taskPrepare, taskCreate, taskStart, taskBootstrapEndpoints } from "../task/task.m"

let main(): Word {
    kernelInit()
    if !consoleInit() {
        panic("console initialization failed", null)
        return 1
    }
    setPanicStage("running")
    print("LA/IX\n")

    setPanicStage("task-prepare")
    if !taskPrepare() || taskCreate() != 2 || !taskBootstrapEndpoints() {
        panic("could not prepare user tasks", null)
        return 1
    }
    setPanicStage("user-task")
    taskStart(kernelBootInfo.clock)
    panic("user task entry returned", null)
    return 1
}

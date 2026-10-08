import { kernelInit, kernelBootInfo } from "../../../src/kernel/boot.m"
import { panic, setPanicStage } from "../../../src/kernel/panic.m"
import { taskStart } from "../../../src/task/task.m"
import { supervisorBootstrap } from "supervisor_bootstrap.m"

let main(): Word {
    kernelInit()
    setPanicStage("supervisor-prepare")
    if !supervisorBootstrap() {
        panic("could not prepare user supervisor", null)
        return 1
    }
    setPanicStage("user-supervisor")
    taskStart(kernelBootInfo.clock)
    panic("supervisor entry returned", null)
    return 1
}

import { kernelInit, kernelBootInfo } from "boot.m"
import { panic, setPanicStage } from "panic.m"
import { taskStart } from "../task/task.m"
import { soakBootstrap } from "soak_bootstrap.m"

let main(): Word {
    kernelInit()
    setPanicStage("soak-prepare")
    if !soakBootstrap() {
        panic("could not prepare soak supervisor", null)
        return 1
    }
    setPanicStage("user-soak")
    taskStart(kernelBootInfo.clock)
    panic("soak entry returned", null)
    return 1
}

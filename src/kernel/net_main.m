import { kernelInit, kernelBootInfo } from "boot.m"
import { panic, setPanicStage } from "panic.m"
import { taskStart } from "../task/task.m"
import { bootstrapNetInit } from "net_bootstrap.m"
let main(): Word {
    kernelInit()
    setPanicStage("net-services")
    if !bootstrapNetInit() {
        panic("could not prepare network services", null)
        return 1
    }
    setPanicStage("user-task")
    taskStart(kernelBootInfo.clock)
    panic("network entry returned", null)
    return 1
}

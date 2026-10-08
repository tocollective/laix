// Kernel entry of the single image: bring up the kernel, create init, run.
import { kernelInit, kernelBootInfo } from "boot.m"
import { panic, setPanicStage } from "panic.m"
import { taskStart } from "../task/task.m"
import { initBootstrap } from "init_bootstrap.m"

let main(): Word {
    kernelInit()
    setPanicStage("init-prepare")
    if !initBootstrap() {
        panic("could not prepare init", null)
        return 1
    }
    setPanicStage("user-init")
    taskStart(kernelBootInfo.clock)
    panic("init entry returned", null)
    return 1
}

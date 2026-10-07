// Recovery boot grants policy roots; all launch order and retries are user code.
import { kernelInit, kernelBootInfo } from "boot.m"
import { taskCreateProgram } from "../task/program.m"
import { taskGet, Task, taskPublish, taskStart } from "../task/task.m"
import { taskInstallRuntimeStart, taskControlBootstrapSelf, taskCatalogLoad, ImageRow } from "../task/control.m"
import { serviceBootstrap } from "../task/recovery.m"
import { endpointFactoryBootstrap, ENDPOINT_FACTORY_QUOTA } from "../ipc/objects.m"
import { DEVICE_DISK } from "../arch/wrm081632/defs.m"
import { panic, setPanicStage } from "panic.m"
extern let recoveryPolicyImage: UByte
extern let recoveryPolicyEnd: UByte
// Build-issued rows for catalog images 2.. (see recovery_main.asm).
extern let recoveryCatalog: UByte
extern let recoveryCatalogEnd: UByte
let main(): Word {
    kernelInit()
    setPanicStage("service-recovery")
    let supervisor: UWord = taskCreateProgram(&recoveryPolicyImage as UWord, &recoveryPolicyEnd as UWord)
    let rows: UWord = &recoveryCatalog as UWord
    let images: UWord = taskCatalogLoad(rows as *ImageRow, (&recoveryCatalogEnd as UWord - rows) / 8)
    if supervisor == 0 || images == 0 || !taskInstallRuntimeStart(supervisor, 0, 0, 0) ||
        !taskControlBootstrapSelf(supervisor) || !serviceBootstrap(supervisor) panic("recovery bootstrap failed", null)
    let policy: *mut Task = taskGet(supervisor)
    policy.createImages = images
    policy.deviceFactory = DEVICE_DISK
    if !endpointFactoryBootstrap(&mut policy.handles, 3, ENDPOINT_FACTORY_QUOTA, true) ||
        !taskPublish(supervisor) panic("recovery root grant failed", null)
    taskStart(kernelBootInfo.clock)
    return 1
}

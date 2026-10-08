// Supervised display boot: the supervisor receives image creation (Echo, bitmap
// storage, Screen, itself) and the Screen and font-extent device factories. All
// launch order, retry and reconnect policy is user code; no screen or storage
// task exists before the supervisor runs, and nothing here names an image.
import { kernelInit, kernelBootInfo } from "../../../src/kernel/boot.m"
import { taskCreateProgram } from "../../../src/task/program.m"
import { taskGet, Task, taskPublish, taskStart } from "../../../src/task/task.m"
import { taskInstallRuntimeStart, taskControlBootstrapSelf, taskCatalogLoad, ImageRow } from "../../../src/task/control.m"
import { serviceBootstrap } from "../../../src/task/recovery.m"
import { endpointFactoryBootstrap, ENDPOINT_FACTORY_QUOTA } from "../../../src/ipc/objects.m"
import { DEVICE_SCREEN, DEVICE_FONT } from "../../../src/arch/wrm081632/defs.m"
import { panic, setPanicStage } from "../../../src/kernel/panic.m"
extern let screenRecoveryPolicyImage: UByte
extern let screenRecoveryPolicyEnd: UByte
// Build-issued rows for catalog images 2.. (see screen_recovery_main.asm).
extern let screenRecoveryCatalog: UByte
extern let screenRecoveryCatalogEnd: UByte
let main(): Word {
    kernelInit()
    setPanicStage("screen-recovery")
    let supervisor: UWord = taskCreateProgram(&screenRecoveryPolicyImage as UWord, &screenRecoveryPolicyEnd as UWord)
    let rows: UWord = &screenRecoveryCatalog as UWord
    let images: UWord = taskCatalogLoad(rows as *ImageRow, (&screenRecoveryCatalogEnd as UWord - rows) / 8)
    if supervisor == 0 || images == 0 || !taskInstallRuntimeStart(supervisor, 0, 0, 0) ||
        !taskControlBootstrapSelf(supervisor) || !serviceBootstrap(supervisor) panic("screen recovery bootstrap failed", null)
    let policy: *mut Task = taskGet(supervisor)
    policy.createImages = images
    policy.deviceFactory = DEVICE_SCREEN | DEVICE_FONT
    if !endpointFactoryBootstrap(&mut policy.handles, 3, ENDPOINT_FACTORY_QUOTA, true) ||
        !taskPublish(supervisor) panic("screen recovery root grant failed", null)
    taskStart(kernelBootInfo.clock)
    return 1
}

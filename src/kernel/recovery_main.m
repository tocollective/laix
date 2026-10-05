// Recovery boot grants policy roots; all launch order and retries are user code.
import { kernelInit, kernelBootInfo } from "boot.m"
import { taskCreateProgram } from "../task/program.m"
import { taskGet, Task, taskPublish, taskStart } from "../task/task.m"
import { taskInstallRuntimeStart, taskControlBootstrapSelf, taskRegisterImage } from "../task/control.m"
import { serviceBootstrap } from "../task/recovery.m"
import { endpointFactoryBootstrap, ENDPOINT_FACTORY_QUOTA } from "../ipc/objects.m"
import { DEVICE_DISK } from "../arch/wrm081632/defs.m"
import { panic, setPanicStage } from "panic.m"
extern let recoveryEchoImage: UByte
extern let recoveryEchoEnd: UByte
extern let recoveryDiskImage: UByte
extern let recoveryDiskEnd: UByte
extern let recoveryFilesImage: UByte
extern let recoveryFilesEnd: UByte
extern let recoveryPolicyImage: UByte
extern let recoveryPolicyEnd: UByte
let main(): Word {
    kernelInit()
    setPanicStage("service-recovery")
    let supervisor: UWord = taskCreateProgram(&recoveryPolicyImage as UWord, &recoveryPolicyEnd as UWord)
    if supervisor == 0 || !taskInstallRuntimeStart(supervisor, 0, 0, 0) ||
        !taskControlBootstrapSelf(supervisor) || !serviceBootstrap(supervisor) ||
        !taskRegisterImage(2, &recoveryEchoImage as UWord, &recoveryEchoEnd as UWord) ||
        !taskRegisterImage(3, &recoveryDiskImage as UWord, &recoveryDiskEnd as UWord) ||
        !taskRegisterImage(4, &recoveryFilesImage as UWord, &recoveryFilesEnd as UWord) ||
        !taskRegisterImage(5, &recoveryPolicyImage as UWord, &recoveryPolicyEnd as UWord) panic("recovery bootstrap failed", null)
    let policy: *mut Task = taskGet(supervisor)
    policy.createImages = 31
    policy.deviceFactory = DEVICE_DISK
    if !endpointFactoryBootstrap(&mut policy.handles, 3, ENDPOINT_FACTORY_QUOTA, true) ||
        !taskPublish(supervisor) panic("recovery root grant failed", null)
    taskStart(kernelBootInfo.clock)
    return 1
}

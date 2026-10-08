// The only trusted boot policy: one init task, the root server. Everything else
// is created by init itself, through the runtime task, endpoint, memory and
// device calls, from the image catalog below. The kernel keeps no per-profile
// task graph, handle wiring or device grant.
//
// Init receives, and nobody else ever holds:
//   * creation of every catalog image and the load authority for images read
//     from storage (Task.createImages);
//   * every device broker operation (Task.deviceFactory); the broker still
//     hands each grant to one unpublished child at a time;
//   * an endpoint factory and the recovery quota exemptions of a supervisor;
//   * control of itself, and the service registry for supervised restarts.
import { taskCreateProgram } from "../task/program.m"
import { taskInitAvailable, taskGet, taskPublish, taskDiscardCreated, Task } from "../task/task.m"
import { taskInstallRuntimeStart, taskControlBootstrapSelf, taskCatalogLoad, ImageRow,
    taskReleaseSupervisor } from "../task/control.m"
import { serviceBootstrap } from "../task/recovery.m"
import { endpointFactoryBootstrap, ENDPOINT_FACTORY_QUOTA } from "../ipc/objects.m"
import { DEVICE_UART_TX, DEVICE_INPUT, DEVICE_DISK, DEVICE_FONT, DEVICE_SCREEN, DEVICE_NET,
    IMAGE_LOAD_AUTHORITY } from "../arch/wrm081632/defs.m"
import { panic } from "panic.m"
import { approvedSession } from "../drivers/resources.m"

extern let initImage: UByte
extern let initImageEnd: UByte
// Build-issued rows for catalog images 2.. in order (init_bootstrap.asm). The
// order is the contract with user/init/images.m.
extern let initCatalog: UByte
extern let initCatalogEnd: UByte

let initDiscard(reference: UWord): Bool {
    taskReleaseSupervisor(reference)
    if !taskDiscardCreated(reference) panic("could not discard init", null)
    return false
}

let initBootstrap(): Bool {
    if !taskInitAvailable() return false
    let init: UWord = taskCreateProgram(&initImage as UWord, &initImageEnd as UWord)
    if init == 0 return false
    let rows: UWord = &initCatalog as UWord
    let images: UWord = taskCatalogLoad(rows as *ImageRow, (&initCatalogEnd as UWord - rows) / 8)
    if images == 0 return initDiscard(init)
    // The session the build selected is init's start argument (user/init/sessions.m).
    if !taskInstallRuntimeStart(init, 0, 0, approvedSession()) || !taskControlBootstrapSelf(init) || !serviceBootstrap(init) {
        return initDiscard(init)
    }
    let root: *mut Task = taskGet(init)
    root.createImages = images | IMAGE_LOAD_AUTHORITY
    root.deviceFactory = DEVICE_UART_TX | DEVICE_INPUT | DEVICE_DISK | DEVICE_FONT | DEVICE_SCREEN | DEVICE_NET
    if !endpointFactoryBootstrap(&mut root.handles, 3, ENDPOINT_FACTORY_QUOTA, true) return initDiscard(init)
    if !taskPublish(init) return initDiscard(init)
    return true
}
export { initBootstrap }

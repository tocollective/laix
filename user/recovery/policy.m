// Private supervisor policy: Echo and Disk -> Files, four replacements per boot.
// Deadlines and sleep bound watchdog work; no registry entry pins a stale root.
import { TaskEvent } from "../../src/task/runtime_start.m"
import { createTask, createEndpoint, configureService, publishService,
    withdrawService, grantTaskDevices, closeHandle, terminateTask, inspectTask,
    collectTask, sleep } from "../syscalls.m"
import { ENDPOINT_MODE_SERVICE, DEVICE_DISK, TASK_EVENT_RECLAIMED,
    ERRNO_EAGAIN, ERRNO_EPIPE, ERRNO_EBUSY, ERRNO_ESRCH } from "../../src/arch/wrm081632/defs.m"

type ManagedService {
    reference: UWord,
    root: UWord,
    generation: UWord,
    attempts: UWord,
    unavailable: Bool,
}
let mut recoveryEvent: TaskEvent

// Every failure discards unpublished construction or terminates a published
// child. Discovery is committed only after image, mappings and grants succeed.
let launchService(service: *mut ManagedService, image: UWord, name: UWord,
    dependency: UWord, generation: UWord, devices: UWord): Word {
    if service.unavailable || service.attempts >= 5 return -ERRNO_EPIPE
    service.attempts += 1
    let child: Word = createTask(image)
    if child < 0 return child
    let root: Word = createEndpoint(ENDPOINT_MODE_SERVICE, child as UWord)
    let mut result: Word = root
    let mut irq: Word = 0
    if root > 0 {
        result = 0
        if devices != 0 irq = grantTaskDevices(child as UWord, devices)
        if irq < 0 result = irq
        if result == 0 result = configureService(child as UWord, root as UWord,
            dependency, generation, irq as UWord)
        if result == 0 result = publishService(name, child as UWord, root as UWord, generation)
    }
    if result < 0 {
        // configure may already have rolled back the entire construction.
        let stopped: Word = terminateTask(child as UWord, result)
        if stopped == 0 {
            for wait: UWord in 0..5 {
                let read: Word = inspectTask(child as UWord, &mut recoveryEvent)
                if read != 0 break
                if recoveryEvent.flags & TASK_EVENT_RECLAIMED != 0 {
                    let collected: Word = collectTask(child as UWord, &mut recoveryEvent)
                    if collected != 0 return collected
                    break
                }
                let paused: Word = sleep(1)
                if paused != 0 return paused
            }
        }
        if root > 0 {
            let closed: Word = closeHandle(root as UWord)
            if closed != 0 return closed
        }
        return result
    }
    service.reference = child as UWord
    service.root = root as UWord
    service.generation = generation
    return 0
}

// Withdraw consumers before producers; quiescent producers launch first.
// A five-second quarantine budget never claims to abort physical DMA.
let retireService(service: *mut ManagedService, name: UWord): Word {
    let hidden: Word = withdrawService(name, -ERRNO_EAGAIN)
    if hidden != 0 return hidden
    if service.reference == 0 return 0
    let stopped: Word = terminateTask(service.reference, -ERRNO_EPIPE)
    if stopped != 0 && stopped != -ERRNO_ESRCH return stopped
    // Death may already have occurred. Inspect remains authorized until collect.
    for wait: UWord in 0..5 {
        let status: Word = inspectTask(service.reference, &mut recoveryEvent)
        if status != 0 return status
        if recoveryEvent.flags & TASK_EVENT_RECLAIMED != 0 {
            let collected: Word = collectTask(service.reference, &mut recoveryEvent)
            if collected != 0 return collected
            let closed: Word = closeHandle(service.root)
            if closed != 0 return closed
            service.reference = 0
            service.root = 0
            return 0
        }
        let paused: Word = sleep(1)
        if paused != 0 return paused
    }
    // Keep the completion capability while the dead owner's DMA is pinned.
    // No further automatic retry/regrant occurs after this terminal state.
    service.unavailable = true
    let quarantined: Word = withdrawService(name, -ERRNO_EPIPE)
    if quarantined != 0 return quarantined
    return -ERRNO_EBUSY
}

// Bounded exponential backoff: 1, 2, 4, 8 seconds; lifetime attempt budget.
let recoveryBackoff(attempt: UWord): Word {
    if attempt >= 5 return -ERRNO_EPIPE
    let mut shift: UWord = attempt
    if shift > 3 shift = 3
    return sleep(1 << shift)
}
export { ManagedService, launchService, retireService, recoveryBackoff }

// Poll completion/fault events once per watchdog tick. A liveness report from
// a consenting client may force the same bounded path for a stalled live task.
let serviceFailed(service: *ManagedService): Bool {
    if service.reference == 0 return true
    let status: Word = inspectTask(service.reference, &mut recoveryEvent)
    return status != 0 || recoveryEvent.state == 3
}
let recoveryUnavailable(service: *mut ManagedService, name: UWord): Word {
    service.unavailable = true
    let hidden: Word = withdrawService(name, -ERRNO_EPIPE)
    if hidden != 0 return hidden
    return -ERRNO_EPIPE
}
let recoverService(service: *mut ManagedService, image: UWord, name: UWord,
    dependency: UWord, devices: UWord): Word {
    let retired: Word = retireService(service, name)
    if retired != 0 return retired
    if service.attempts >= 5 return recoveryUnavailable(service, name)
    let paused: Word = recoveryBackoff(service.attempts - 1)
    if paused != 0 return paused
    return launchService(service, image, name, dependency, service.generation + 1, devices)
}
let recoverFilesDisk(disk: *mut ManagedService, files: *mut ManagedService): Word {
    // Reconstruct all volatile state. Never reuse a downstream dependency handle.
    let downstream: Word = retireService(files, 2)
    if downstream != 0 return downstream
    let upstream: Word = retireService(disk, 3)
    if upstream != 0 return upstream
    if disk.attempts >= 5 || files.attempts >= 5 {
        let disabled: Word = recoveryUnavailable(disk, 3)
        if disabled != -ERRNO_EPIPE return disabled
        return recoveryUnavailable(files, 2)
    }
    let paused: Word = recoveryBackoff(disk.attempts - 1)
    if paused != 0 return paused
    let generation: UWord = disk.generation + 1
    let started: Word = launchService(disk, 3, 3, 0, generation, DEVICE_DISK)
    if started != 0 {
        let disabled: Word = recoveryUnavailable(disk, 3)
        if disabled != -ERRNO_EPIPE return disabled
        return recoveryUnavailable(files, 2)
    }
    let consumer: Word = launchService(files, 4, 2, disk.root, generation, 0)
    if consumer != 0 return recoveryUnavailable(files, 2)
    return 0
}
export { serviceFailed, recoverService, recoverFilesDisk, recoveryUnavailable }

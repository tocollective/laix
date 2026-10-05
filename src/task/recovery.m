import { irqTokenValid } from "../drivers/irq.m"
import { DEVICE_DISK, DEVICE_INPUT, KEYBOARD_IRQ } from "../arch/wrm081632/defs.m"
import { serviceDiskIrq } from "../drivers/service_devices.m"
import { panic } from "../kernel/panic.m"
import { kernelBootInfo } from "../kernel/boot.m"
// Private, bounded resolver mechanism. User code owns restart/dependency policy.
import { currentTask, taskGet, Task, TASK_CREATED, TASK_DEAD } from "task.m"
import { taskControlLookup, taskRuntimeDiscard, taskInstallRuntimeStart, taskRuntimePublish, TaskControl } from "control.m"
import { RecoveryStart, ServiceResolution } from "recovery_start.m"
import { handleLookup, handleCopy, handleClose, ENDPOINT_SERVICE,
    objectAssertAtomic, Endpoint, HandleTable } from "../ipc/objects.m"
import { copyToUser } from "../mm/mmu.m"
import { TASK_RIGHT_CONFIGURE, TASK_RIGHT_INSPECT, RIGHT_SEND, RIGHT_RECEIVE,
    RIGHT_MANAGE, ERRNO_EPERM, ERRNO_EINVAL, ERRNO_EBUSY, ERRNO_ENFILE,
    ERRNO_EAGAIN, ERRNO_EPIPE, RECOVERY_START_BYTES } from "../arch/wrm081632/defs.m"

let SERVICE_ROWS: UWord = 8
let SERVICE_NAMES: UWord = 4
// No namespace root can be created at runtime or inferred from an integer.
let mut serviceSupervisors: UWord[8]
type ServiceEntry {
    owner: UWord,
    name: UWord,
    reference: UWord,
    root: UWord,
    generation: UWord, // survives withdrawal; never wrap/reuse within this owner
    status: Word,
}
let mut serviceEntries: ServiceEntry[SERVICE_ROWS]
let serviceSupervisor(owner: UWord): Bool {
    for i: UWord in 0..8 if serviceSupervisors[i] == owner && owner != 0 return true
    return false
}
let serviceBootstrap(owner: UWord): Bool {
    objectAssertAtomic()
    let task: *mut Task = taskGet(owner)
    if task == null || task.state != TASK_CREATED || !taskControlLookupBootOpen() return false
    for i: UWord in 0..8 {
        if serviceSupervisors[i] == owner return false
        if serviceSupervisors[i] == 0 {
            serviceSupervisors[i] = owner
            return true
        }
    }
    return false
}
// Uses the task-control seal instead of adding a reopenable bootstrap root.
import { taskControlLookupBootOpen } from "control.m"
let serviceRow(owner: UWord, name: UWord, create: Bool): *mut ServiceEntry {
    let mut free: *mut ServiceEntry = null
    for i: UWord in 0..SERVICE_ROWS {
        let row: *mut ServiceEntry = &mut serviceEntries[i]
        if row.owner == owner && row.name == name return row
        if row.owner == 0 && free == null free = row
    }
    if !create || free == null return null
    free.owner = owner
    free.name = name
    free.status = -ERRNO_EAGAIN
    return free
}
let serviceReleaseSupervisor(owner: UWord): Void {
    for i: UWord in 0..8 if serviceSupervisors[i] == owner serviceSupervisors[i] = 0
    for i: UWord in 0..SERVICE_ROWS {
        if serviceEntries[i].owner != owner continue
        serviceEntries[i].owner = 0
        serviceEntries[i].reference = 0
        serviceEntries[i].root = 0
        serviceEntries[i].generation = 0
    }
}
let serviceAllow(reference: UWord, mask: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || !serviceSupervisor(currentTask.id) ||
        taskControlLookup(reference, TASK_RIGHT_CONFIGURE) == null return -ERRNO_EPERM
    let task: *mut Task = taskGet(reference)
    if task == null || task.state != TASK_CREATED || task.configured return -ERRNO_EBUSY
    if mask & ~(15 as UWord) != 0 return -ERRNO_EINVAL
    task.resolverOwner = currentTask.id
    task.resolverMask = mask
    return 0
}
let serviceWithdraw(name: UWord, status: Word): Word {
    objectAssertAtomic()
    if currentTask == null || !serviceSupervisor(currentTask.id) return -ERRNO_EPERM
    if name == 0 || name > SERVICE_NAMES || (status != -ERRNO_EAGAIN && status != -ERRNO_EPIPE) return -ERRNO_EINVAL
    let row: *mut ServiceEntry = serviceRow(currentTask.id, name, true)
    if row == null return -ERRNO_ENFILE
    row.root = 0
    row.reference = 0
    row.status = status
    return 0
}
let serviceDependencyLive(task: *Task): Bool {
    let block: *RecoveryStart = task.bootPage as *RecoveryStart
    if block.bytes != RECOVERY_START_BYTES || block.dependency == 0 return true
    let upstream: *mut Endpoint = handleLookup(&task.handles as *mut HandleTable, block.dependency, RIGHT_SEND)
    if upstream == null return false
    let peer: *mut Task = taskGet(upstream.manager)
    if peer == null || peer.state == TASK_CREATED || peer.state == TASK_DEAD || peer.bootPage == 0 return false
    let dependency: *RecoveryStart = peer.bootPage as *RecoveryStart
    return dependency.bytes == RECOVERY_START_BYTES && dependency.generation == block.generation
}

let servicePublish(name: UWord, reference: UWord, root: UWord, generation: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || !serviceSupervisor(currentTask.id) ||
        taskControlLookup(reference, TASK_RIGHT_INSPECT) == null return -ERRNO_EPERM
    if name == 0 || name > SERVICE_NAMES || generation == 0 || generation > 0x7FFFFFFF return -ERRNO_EINVAL
    let task: *mut Task = taskGet(reference)
    if task == null || task.state == TASK_DEAD || !task.configured return -ERRNO_EBUSY
    let endpoint: *mut Endpoint = handleLookup(&mut currentTask.handles, root, RIGHT_SEND | RIGHT_MANAGE)
    if endpoint == null || endpoint.mode != ENDPOINT_SERVICE || endpoint.manager != reference return -ERRNO_EPERM
    let block: *RecoveryStart = task.bootPage as *RecoveryStart
    if block.bytes != RECOVERY_START_BYTES || block.generation != generation || !serviceDependencyLive(task) ||
        handleLookup(&mut task.handles, block.endpoint, RIGHT_RECEIVE) != endpoint return -ERRNO_EINVAL
    // Validate everything before reserving/publishing the row. No partial entry.
    let previous: *mut ServiceEntry = serviceRow(currentTask.id, name, false)
    if previous != null {
        if generation <= previous.generation return -ERRNO_EINVAL
        if previous.root != 0 && handleLookup(&mut currentTask.handles, previous.root, RIGHT_SEND) != null return -ERRNO_EBUSY
    }
    // Check registry capacity before making a Created task schedulable.
    if previous == null {
        let mut free: Bool = false
        for i: UWord in 0..SERVICE_ROWS if serviceEntries[i].owner == 0 free = true
        if !free return -ERRNO_ENFILE
    }
    // Task publication and resolver commit share this IRQ-excluded syscall.
    // No client or new server can observe the interval between these stores.
    if task.state == TASK_CREATED {
        let published: Word = taskRuntimePublish(reference)
        if published != 0 return published
    }
    let row: *mut ServiceEntry = serviceRow(currentTask.id, name, true)
    if row == null {
        panic("reserved resolver publication disappeared", null)
        return -ERRNO_ENFILE
    }
    row.reference = reference
    row.root = root
    row.generation = generation
    row.status = 0
    return 0
}
let serviceResolve(name: UWord, destination: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || name == 0 || name > SERVICE_NAMES return -ERRNO_EINVAL
    let owner: UWord = currentTask.resolverOwner
    if !serviceSupervisor(owner) || currentTask.resolverMask & (1 << (name - 1)) == 0 return -ERRNO_EPERM
    let row: *mut ServiceEntry = serviceRow(owner, name, false)
    if row == null return -ERRNO_EAGAIN
    if row.status != 0 return row.status
    let supervisor: *mut Task = taskGet(owner)
    let server: *mut Task = taskGet(row.reference)
    if supervisor == null || server == null || server.state == TASK_DEAD ||
        handleLookup(&mut supervisor.handles, row.root, RIGHT_SEND) == null return -ERRNO_EAGAIN
    // Consent is this syscall. Only the calling task's table receives a copy.
    let token: Word = handleCopy(&mut supervisor.handles, row.root, &mut currentTask.handles,
        owner, currentTask.id, RIGHT_SEND)
    if token < 0 return token
    let mut result: ServiceResolution
    result.handle = token as UWord
    result.instance = row.reference
    result.generation = row.generation
    result.name = name
    let copied: Word = copyToUser(currentTask.directory, currentTask.id, destination,
        &result as *UByte, sizeof(ServiceResolution))
    if copied != 0 {
        if handleClose(&mut currentTask.handles, token as UWord) != 0 panic("resolver rollback failed", null)
        return copied
    }
    return 0
}
let serviceConfigure(reference: UWord, root: UWord, dependency: UWord, generation: UWord, irq: UWord): Word {
    objectAssertAtomic()
    let control: *mut TaskControl = taskControlLookup(reference, TASK_RIGHT_CONFIGURE)
    if currentTask == null || !serviceSupervisor(currentTask.id) || control == null return -ERRNO_EPERM
    let task: *mut Task = taskGet(reference)
    if task == null || task.state != TASK_CREATED || task.configured return -ERRNO_EBUSY
    if generation == 0 || generation > 0x7FFFFFFF return -ERRNO_EINVAL
    let endpoint: *mut Endpoint = handleLookup(&mut currentTask.handles, root, RIGHT_RECEIVE | RIGHT_MANAGE)
    if endpoint == null || endpoint.mode != ENDPOINT_SERVICE || endpoint.manager != reference return -ERRNO_EPERM
    if dependency != 0 {
        let upstream: *mut Endpoint = handleLookup(&mut currentTask.handles, dependency, RIGHT_SEND)
        if upstream == null || upstream.mode != ENDPOINT_SERVICE return -ERRNO_EPERM
        let peer: *mut Task = taskGet(upstream.manager)
        if peer == null || peer.state == TASK_CREATED || peer.state == TASK_DEAD || peer.bootPage == 0 return -ERRNO_EBUSY
        let block: *RecoveryStart = peer.bootPage as *RecoveryStart
        if block.bytes != RECOVERY_START_BYTES || block.generation != generation return -ERRNO_EINVAL
    }
    if ((task.deviceRights == DEVICE_DISK && !irqTokenValid(reference, irq, serviceDiskIrq(kernelBootInfo.disk))) ||
        (task.deviceRights == DEVICE_INPUT && !irqTokenValid(reference, irq, KEYBOARD_IRQ)) ||
        (task.deviceRights == 0 && irq != 0)) return -ERRNO_EINVAL
    let received: Word = handleCopy(&mut currentTask.handles, root, &mut task.handles,
        currentTask.id, reference, RIGHT_RECEIVE)
    if received < 0 return received
    let mut dep: UWord = 0
    if dependency != 0 {
        let copied: Word = handleCopy(&mut currentTask.handles, dependency, &mut task.handles,
            currentTask.id, reference, RIGHT_SEND)
        if copied < 0 return taskRuntimeDiscard(control)
        dep = copied as UWord
    }
    if !taskInstallRuntimeStart(reference, received as UWord, RIGHT_RECEIVE, generation) return taskRuntimeDiscard(control)
    let block: *mut RecoveryStart = task.bootPage as *mut RecoveryStart
    block.bytes = RECOVERY_START_BYTES
    block.dependency = dep
    block.irq = irq
    block.generation = generation
    block.supervisor = currentTask.id
    task.context.regs[2] = RECOVERY_START_BYTES
    return 0
}
export { serviceDependencyLive, serviceBootstrap, servicePublish, serviceResolve, serviceAllow,
    serviceWithdraw, serviceConfigure, serviceReleaseSupervisor }

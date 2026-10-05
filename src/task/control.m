import { taskConstructProgram } from "program.m"
import { serviceReleaseSupervisor, serviceDependencyLive } from "recovery.m"
import { irqIssue, irqReleaseTask } from "../drivers/irq.m"
import { diskDevicesCheck, diskDevicesRegrant, serviceDiskIrq } from "../drivers/service_devices.m"
import { kernelBootInfo } from "../kernel/boot.m"
import { inputDevicesInit } from "../drivers/input_device.m"
import { DEVICE_UART_TX, DEVICE_INPUT, DEVICE_DISK, KEYBOARD_IRQ } from "../arch/wrm081632/defs.m"
// Bounded, nontransferable task capabilities and completion mailboxes.
// References select records; only the kernel-recorded owner grants authority.
import { Task, currentTask, taskGet, taskConstructImage, taskPublishChecked,
    taskDiscardChecked, taskTerminateChecked, taskSaveContext, taskOwnsTrap,
    TASK_CREATED, TASK_DEAD, USER_DATA } from "task.m"
import { RuntimeStart, TaskEvent } from "runtime_start.m"
import { TrapFrame } from "../trap/trap_frame.m"
import { PAGE_NONE, PAGE_USER, allocPage } from "../mm/memory.m"
import { mapPage, copyToUser } from "../mm/mmu.m"
import { handleCopy, handleLookup } from "../ipc/objects.m"
import { objectAssertAtomic } from "../ipc/objects.m"
import { serviceDevicesQuiescent } from "../drivers/service_devices.m"
import { panic } from "../kernel/panic.m"
import { TASK_RIGHT_CONFIGURE, TASK_RIGHT_PUBLISH, TASK_RIGHT_INSPECT,
    TASK_RIGHT_TERMINATE, TASK_RIGHT_COLLECT, TASK_RIGHT_ALL,
    TASK_EVENT_FAULT, TASK_EVENT_TERMINATED, TASK_EVENT_RECLAIMED,
    TASK_EVENT_QUARANTINED, RUNTIME_START_MAGIC, RUNTIME_START_VERSION,
    RUNTIME_START_BYTES, START_BLOCK_VA, PAGE_SIZE, PTE_RO, PTE_U,
    RIGHT_ALL, ERRNO_EPERM, ERRNO_ESRCH, ERRNO_EINVAL, ERRNO_ENFILE,
    ERRNO_EAGAIN, ERRNO_EBUSY } from "../arch/wrm081632/defs.m"

let MAX_TASK_CONTROLS: UWord = 16 // also bounds uncollected completion events
let TASK_HISTORY_SIZE: UWord = 32 // diagnostic ring; never used as authority

type TaskControl {
    owner: UWord, // supervisor reference, not its diagnostic slot
    reference: UWord,
    rights: UWord,
    done: Bool,
    event: TaskEvent,
}
let mut taskControls: TaskControl[MAX_TASK_CONTROLS]
let mut taskHistory: TaskEvent[TASK_HISTORY_SIZE]
let mut taskHistoryHead: UWord
let mut taskControlsSealed: Bool
let mut taskHistoryCount: UWord

extern let runtimeApprovedStart: UByte
extern let runtimeApprovedEnd: UByte

// Immutable boot catalog: registration closes with task-control sealing.
let mut runtimeImageStart: UWord[5]
let mut runtimeImageEnd: UWord[5]
let taskRegisterImage(image: UWord, start: UWord, end: UWord): Bool {
    objectAssertAtomic()
    if taskControlsSealed || image < 2 || image > 5 || start == 0 || end <= start ||
        runtimeImageStart[image - 1] != 0 return false
    runtimeImageStart[image - 1] = start
    runtimeImageEnd[image - 1] = end
    return true
}

let taskControlLookup(reference: UWord, rights: UWord): *mut TaskControl {
    objectAssertAtomic()
    if currentTask == null || currentTask.id == 0 || rights == 0 return null
    for i: UWord in 0..MAX_TASK_CONTROLS {
        let control: *mut TaskControl = &mut taskControls[i]
        if control.reference == reference && reference != 0 && control.owner == currentTask.id &&
            control.rights & rights == rights return control
    }
    return null
}

let taskControlFree(): *mut TaskControl {
    for i: UWord in 0..MAX_TASK_CONTROLS {
        if taskControls[i].reference == 0 return &mut taskControls[i]
    }
    return null
}

// Bootstrap may grant specific control independently of creation authority.
// Runtime callers cannot invoke this kernel-only policy or forge table rows.
let taskControlBootstrap(owner: UWord, reference: UWord, rights: UWord): Bool {
    objectAssertAtomic()
    let task: *mut Task = taskGet(reference)
    let supervisor: *mut Task = taskGet(owner)
    let control: *mut TaskControl = taskControlFree()
    if taskControlsSealed || task == null || task.state != TASK_CREATED || supervisor == null ||
        supervisor.state != TASK_CREATED || control == null || rights == 0 || rights & ~TASK_RIGHT_ALL != 0 return false
    for i: UWord in 0..MAX_TASK_CONTROLS {
        if taskControls[i].reference == reference return false
    }
    control.owner = owner
    control.reference = reference
    control.rights = rights
    control.done = false
    return true
}

let taskControlBootstrapSelf(reference: UWord): Bool {
    return taskControlBootstrap(reference, reference, TASK_RIGHT_INSPECT | TASK_RIGHT_TERMINATE)
}

let taskControlLookupBootOpen(): Bool { return !taskControlsSealed }
let taskControlSeal(): Void { taskControlsSealed = true }

// Image IDs are catalog choices, never addresses. Creation authority is an
// image mask on the caller; the new per-object capability controls only child.
let taskRuntimeCreate(image: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || image == 0 || image > 5 ||
        currentTask.createImages & (1 << (image - 1)) == 0 ||
        (image != 1 && runtimeImageStart[image - 1] == 0) return -ERRNO_EPERM
    let control: *mut TaskControl = taskControlFree()
    if control == null return -ERRNO_ENFILE
    // Reserve a mailbox before any fallible resource allocation. No task can
    // exit without its completion storage, even when the supervisor is slow.
    control.owner = currentTask.id
    control.rights = TASK_RIGHT_ALL
    control.done = false
    let mut reference: UWord = 0
    if image == 1 reference = taskConstructImage(&runtimeApprovedStart as UWord, &runtimeApprovedEnd as UWord, 0)
    else reference = taskConstructProgram(runtimeImageStart[image - 1], runtimeImageEnd[image - 1])
    if reference == 0 {
        control.owner = 0
        control.rights = 0
        return -ERRNO_ENFILE
    }
    control.reference = reference
    let task: *mut Task = taskGet(reference)
    task.reusable = true
    task.resolverOwner = currentTask.id
    return reference as Word
}

let taskRuntimeDiscard(control: *mut TaskControl): Word {
    if !taskDiscardChecked(control.reference) panic("runtime rollback failed", null)
    control.reference = 0
    control.owner = 0
    control.rights = 0
    control.done = false
    return -ERRNO_ENFILE
}

// Optional endpoint rights are attenuated from the supervisor's own table.
// No device/IRQ/create authority is inferred from this startup record.
let taskRuntimeConfigure(reference: UWord, token: UWord, rights: UWord, argument: UWord): Word {
    let control: *mut TaskControl = taskControlLookup(reference, TASK_RIGHT_CONFIGURE)
    if control == null return -ERRNO_EPERM
    let task: *mut Task = taskGet(reference)
    if task == null || task.state != TASK_CREATED || task.configured return -ERRNO_EBUSY
    if ((token == 0 && rights != 0) || (token != 0 && (rights == 0 || rights & ~RIGHT_ALL != 0))) return -ERRNO_EINVAL
    if token != 0 && handleLookup(&mut currentTask.handles, token, rights) == null return -ERRNO_EPERM
    let mut childToken: UWord = 0
    if token != 0 {
        let copied: Word = handleCopy(&mut currentTask.handles, token, &mut task.handles,
            currentTask.id, reference, rights)
        if copied < 0 return copied
        childToken = copied as UWord
    }
    if !taskInstallRuntimeStart(reference, childToken, rights, argument) return taskRuntimeDiscard(control)
    return 0
}

// Checked startup mechanism shared by runtime control and bootstrap policy.
let taskInstallRuntimeStart(reference: UWord, token: UWord, rights: UWord, argument: UWord): Bool {
    objectAssertAtomic()
    let task: *mut Task = taskGet(reference)
    if task == null || task.state != TASK_CREATED || task.bootPage != PAGE_NONE || task.configured ||
        (token == 0 && rights != 0) || (token != 0 && handleLookup(&mut task.handles, token, rights) == null) return false
    task.bootPage = allocPage(reference, PAGE_USER)
    if task.bootPage == PAGE_NONE return false
    let block: *mut RuntimeStart = task.bootPage as *mut RuntimeStart
    block.magic = RUNTIME_START_MAGIC
    block.version = RUNTIME_START_VERSION
    block.bytes = RUNTIME_START_BYTES
    block.reference = reference
    block.slot = task.slot
    block.data = USER_DATA
    block.dataBytes = PAGE_SIZE
    block.endpoint = token
    block.rights = rights
    block.argument = argument
    if !mapPage(task.directory, reference, START_BLOCK_VA, task.bootPage, PTE_RO | PTE_U) return false
    task.context.regs[1] = START_BLOCK_VA
    task.context.regs[2] = RUNTIME_START_BYTES
    task.configured = true
    return true
}

// Scoped brokers: UART TX, keyboard events and quiescent read-only boot Disk.
// Screen/font retain their separate boot-only policy.
let taskRuntimeDevices(reference: UWord, devices: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || devices == 0 ||
        devices & ~(DEVICE_UART_TX | DEVICE_INPUT | DEVICE_DISK) != 0 ||
        (devices & DEVICE_DISK != 0 && devices != DEVICE_DISK) ||
        currentTask.deviceFactory & devices != devices return -ERRNO_EPERM
    if taskControlLookup(reference, TASK_RIGHT_CONFIGURE) == null return -ERRNO_EPERM
    let child: *mut Task = taskGet(reference)
    if child == null || child.state != TASK_CREATED || child.configured ||
        child.deviceRights != 0 return -ERRNO_EBUSY
    let mut token: UWord = 0
    if devices & DEVICE_INPUT != 0 {
        token = irqIssue(reference, KEYBOARD_IRQ)
        if token == 0 return -ERRNO_EBUSY
        if !inputDevicesInit(reference, token) {
            irqReleaseTask(reference)
            return -ERRNO_EBUSY
        }
    }
    if devices == DEVICE_DISK {
        let available: Word = diskDevicesCheck(kernelBootInfo.disk)
        if available != 0 return available
        token = irqIssue(reference, serviceDiskIrq(kernelBootInfo.disk))
        if token == 0 return -ERRNO_EBUSY
        let result: Word = diskDevicesRegrant(reference, kernelBootInfo.disk, kernelBootInfo.imageSize)
        if result != 0 {
            irqReleaseTask(reference)
            return result
        }
    }
    child.deviceRights = devices
    return token as Word
}

let taskRuntimePublish(reference: UWord): Word {
    let control: *mut TaskControl = taskControlLookup(reference, TASK_RIGHT_PUBLISH)
    if control == null return -ERRNO_EPERM
    let task: *mut Task = taskGet(reference)
    if task == null || task.state != TASK_CREATED || !task.configured return -ERRNO_EBUSY
    let block: *RuntimeStart = task.bootPage as *RuntimeStart
    if block.endpoint != 0 && handleLookup(&mut task.handles, block.endpoint, block.rights) == null return -ERRNO_EPERM
    if !serviceDependencyLive(task) return -ERRNO_EPERM
    if !taskPublishChecked(reference) return -ERRNO_EINVAL
    return 0
}

let taskSnapshot(task: *Task, event: *mut TaskEvent): Void {
    event.reference = task.id
    event.slot = task.slot
    event.state = task.state
    event.code = task.exitCode
    event.flags = 0
    if task.faulted event.flags |= TASK_EVENT_FAULT
    if task.reaped event.flags |= TASK_EVENT_RECLAIMED
    event.cause = task.context.cause
    event.epc = task.context.epc
    event.badaddr = task.context.badaddr
    event.status = task.context.status
    event.ptbr = task.context.ptbr
    event.fcsr = task.context.fcsr
}

let taskRecordCompletion(task: *Task, terminated: Bool): Void {
    objectAssertAtomic()
    let mut event: TaskEvent
    taskSnapshot(task, &mut event)
    if terminated event.flags |= TASK_EVENT_TERMINATED
    if !serviceDevicesQuiescent(task.id) event.flags |= TASK_EVENT_QUARANTINED
    taskHistory[taskHistoryHead] = event
    taskHistoryHead = (taskHistoryHead + 1) % TASK_HISTORY_SIZE
    if taskHistoryCount < TASK_HISTORY_SIZE taskHistoryCount += 1
    for i: UWord in 0..MAX_TASK_CONTROLS {
        let control: *mut TaskControl = &mut taskControls[i]
        if control.reference != task.id continue
        if control.owner == 0 control.reference = 0
        else {
            control.event = event
            control.done = true
        }
    }
}

let taskRecordReaped(reference: UWord): Void {
    for i: UWord in 0..MAX_TASK_CONTROLS {
        if taskControls[i].reference == reference && taskControls[i].done {
            taskControls[i].event.flags |= TASK_EVENT_RECLAIMED
            taskControls[i].event.flags &= ~TASK_EVENT_QUARANTINED
        }
    }
    for i: UWord in 0..TASK_HISTORY_SIZE {
        if taskHistory[i].reference == reference {
            taskHistory[i].flags |= TASK_EVENT_RECLAIMED
            taskHistory[i].flags &= ~TASK_EVENT_QUARANTINED
        }
    }
}

// A dead supervisor cannot leave private construction or uncollectable
// mailboxes behind. Published children continue independently (user policy).
let taskReleaseSupervisor(owner: UWord): Void {
    serviceReleaseSupervisor(owner)
    for i: UWord in 0..MAX_TASK_CONTROLS {
        let control: *mut TaskControl = &mut taskControls[i]
        if control.owner != owner || control.reference == 0 continue
        let task: *mut Task = taskGet(control.reference)
        if task != null && task.state == TASK_CREATED {
            if !taskDiscardChecked(task.id) panic("orphan construction rollback failed", null)
            control.reference = 0
        } else if control.done control.reference = 0
        control.owner = 0
        control.rights = 0
    }
}

// Copy the whole snapshot before consuming a mailbox. EFAULT never loses an
// event; EAGAIN means no completion yet. Collection does not free live DMA.
let taskRuntimeRead(reference: UWord, destination: UWord, collect: Bool): Word {
    let mut rights: UWord = TASK_RIGHT_INSPECT
    if collect rights = TASK_RIGHT_COLLECT
    let control: *mut TaskControl = taskControlLookup(reference, rights)
    if control == null return -ERRNO_EPERM
    let mut event: TaskEvent
    if control.done event = control.event
    else {
        if collect return -ERRNO_EAGAIN
        let task: *mut Task = taskGet(reference)
        if task == null return -ERRNO_ESRCH
        taskSnapshot(task, &mut event)
    }
    let result: Word = copyToUser(currentTask.directory, currentTask.id, destination,
        &event as *UByte, sizeof(TaskEvent))
    if result != 0 return result
    if collect {
        control.reference = 0
        control.owner = 0
        control.rights = 0
        control.done = false
    }
    return 0
}

let taskRuntimeTerminate(frame: *mut TrapFrame, reference: UWord, code: Word): *TrapFrame {
    let control: *mut TaskControl = taskControlLookup(reference, TASK_RIGHT_TERMINATE)
    let mut result: Word = -ERRNO_EPERM
    if control != null {
        let task: *mut Task = taskGet(reference)
        if task == null || task.state == TASK_DEAD result = -ERRNO_ESRCH
        else if task.state == TASK_CREATED {
            if !taskDiscardChecked(reference) panic("could not cancel construction", null)
            control.reference = 0
            control.owner = 0
            control.rights = 0
            result = 0
        } else {
            frame.regs[1] = 0
            let selected: *TrapFrame = taskTerminateChecked(frame, reference, code)
            if selected != frame return selected
            result = 0
        }
    }
    frame.regs[1] = result as UWord
    if taskOwnsTrap(frame) taskSaveContext(frame)
    return frame
}

export { taskControlLookup, TaskControl, taskControls, taskHistory, taskHistoryHead, taskHistoryCount,
    MAX_TASK_CONTROLS, TASK_HISTORY_SIZE, taskControlBootstrapSelf, taskControlBootstrap, taskControlSeal, taskInstallRuntimeStart,
    taskControlLookupBootOpen, taskRegisterImage, taskRuntimeDiscard, taskRuntimeDevices, taskRuntimeCreate, taskRuntimeConfigure, taskRuntimePublish, taskRuntimeRead,
    taskRuntimeTerminate, taskRecordCompletion, taskRecordReaped, taskReleaseSupervisor }

// Preemptive round-robin: one CPU, one thread per task, no nested traps.
import { PAGE_SIZE, WORD_BYTES, GPR_COUNT, REG_SP, STATUS_IE, STATUS_PIE, STATUS_PUM,
    STATUS_EXL, STATUS_UM, CR_STATUS, CR_PTBR, PTBR_ENABLE, PTE_U, PTE_RX, PTE_RW, PTE_RO,
    STACK_CANARY, KERNEL_STACK_BYTES, KERNEL_SP, KERNEL_STACK_BOTTOM,
    KERNEL_STACK_TOP, PIC_ENABLE } from "../arch/wrm081632/defs.m"
import { TrapFrame } from "../trap/trap_frame.m"
import { PAGE_NONE, PAGE_USER, PAGE_USER_STACK, allocPage, allocTaskPages, freePage,
    physicalPageOwned, memoryLock, memoryUnlock } from "../mm/memory.m"
import { USER_VA_START, USER_VA_END, mmuCreateAddressSpace, mmuDestroyAddressSpace,
    mmuSwitchAddressSpace, mmuActivateKernel, mapPage,
    mmuAllocKernelStack, mmuFreeKernelStack } from "../mm/mmu.m"
import { mmuSealResources, mmuScreenResourcesValid } from "../mm/mmu.m"
import { irqReleaseTask, irqSeal, irqTokenValid } from "../drivers/irq.m"
import { fontCancelOwner, fontReap, screenReleaseOwner, serviceDevicesQuiescent } from "../drivers/service_devices.m"
import { inputReleaseOwner } from "../drivers/input_device.m"
import { ServiceStart, serviceStartValid } from "service_start.m"
import { panic } from "../kernel/panic.m"
import { debugPrint } from "../drivers/debug_uart.m"
import { timerReady, timerInit, timerCanSleep } from "../drivers/timer.m"
import { Handle, HandleTable, handlesReleaseTask, endpointBootstrap, handleCopy,
    handleClose, endpointSealBootstrap, handleEntry, handleLookup, ENDPOINT_SERVICE } from "../ipc/objects.m"
import { TaskStart, taskStartBlockValid } from "start.m"
import { ipcCancelTask } from "../ipc/ipc.m"
import { Endpoint } from "../ipc/objects.m"
import { RIGHT_SEND, IPC_MESSAGE_MAX, START_BLOCK_VA, START_BLOCK_BYTES,
    START_ROLE_SERVER, START_ROLE_STORAGE, DEVICE_UART_TX,
    SERVICE_START_BYTES, VIDEO_IRQ, KEYBOARD_IRQ, START_ROLE_INPUT, START_ROLE_DISK,
    START_ROLE_FILE, START_ROLE_CLIENT, START_PROTOCOL_FILE, DEVICE_INPUT, DEVICE_DISK } from "../arch/wrm081632/defs.m"

let SCHEDULER_QUANTUM_HZ: UWord = 100
let MAX_TASKS: UWord = 8
let IDLE_STACK_OWNER: UWord = MAX_TASKS + 1
let TASK_EMPTY: UWord = 0 // unused slot, not a schedulable state
let TASK_READY: UWord = 1
let TASK_RUNNING: UWord = 2
let TASK_DEAD: UWord = 3
let TASK_BLOCKED: UWord = 4
let TASK_CREATED: UWord = 5 // private construction, never in the ready queue
let WAIT_NONE: UWord = 0
let WAIT_EVENT: UWord = 1
let WAIT_IPC_SEND: UWord = 2
let WAIT_IPC_RECEIVE: UWord = 3 // reserved; generic event callers must avoid IPC IDs
let WAIT_IPC_CALL: UWord = 4
let WAIT_IPC_ACCEPT: UWord = 5
let WAIT_IPC_REPLY: UWord = 6
let WAIT_IRQ: UWord = 7
let USER_CODE: UWord = USER_VA_START
let USER_DATA: UWord = USER_CODE + PAGE_SIZE
let USER_STACK_TOP: UWord = USER_VA_END
let USER_STACK_BOTTOM: UWord = USER_STACK_TOP - PAGE_SIZE
let USER_STACK_GUARD: UWord = USER_STACK_BOTTOM - PAGE_SIZE
let TASK_PAGE_COUNT: UWord = 3

type Task {
    id: UWord,
    directory: *mut UWord,
    ptbr: UWord,
    asid: UWord,
    userCode: UWord,
    userData: UWord,
    userStackBottom: UWord,
    userStackTop: UWord,
    kernelStackBottom: UWord,
    kernelStackTop: UWord,
    bootPage: UWord,
    deviceRights: UWord, // kernel-granted narrow operations; never user memory
    context: TrapFrame,
    state: UWord,
    queued: Bool,
    waitReason: UWord,
    exitCode: Word,
    faulted: Bool,
    reaped: Bool,
    pages: UWord[TASK_PAGE_COUNT],
    handles: HandleTable,
    ipcEndpoint: *mut Endpoint, // one pinned wait, independent of handles
    ipcKind: UWord,
    ipcBuffer: UWord, // receive VA only; always translated through this TCB
    ipcSize: UWord, // send length or receive capacity
    ipcMessage: UByte[IPC_MESSAGE_MAX],
    ipcObjectGeneration: UWord,
    ipcCallGeneration: UWord, // persistent, never cleared on completion
    ipcReplyOwner: UWord,
    ipcReplyBuffer: UWord,
    ipcReplyCapacity: UWord,
    ipcPadding: UWord, // keep every TCB's TrapFrame aligned to eight bytes
}

align(8) let mut tasks: Task[MAX_TASKS]
let mut currentTask: *mut Task
let mut readyQueue: UWord[MAX_TASKS] // stable IDs, never user pointers
let mut readyHead: UWord
let mut readyCount: UWord
let mut schedulerStarted: Bool
// ID 0 is kernel-only: idle never occupies a user slot or the ready queue.
align(8) let mut idleTask: Task
let taskPurposes: UWord[TASK_PAGE_COUNT] = [PAGE_USER, PAGE_USER, PAGE_USER_STACK]
extern let taskKernelResume: UByte
extern let userCodeStart: UByte
extern let userCodeEnd: UByte
extern let __start_text: UByte
extern let __stop_text: UByte
extern let trapRestoreFrame(frame: *TrapFrame): Void
extern let taskKernelSp(): UWord

let taskIrqsDisabled(): Bool {
    let status: UWord = mfcr(CR_STATUS)
    return status & STATUS_IE == 0 || status & STATUS_EXL != 0
}

let taskTransitionAllowed(previous: UWord, next: UWord): Bool {
    return ((previous == TASK_EMPTY || previous == TASK_CREATED) && next == TASK_READY) ||
        (previous == TASK_READY && next == TASK_RUNNING) ||
        (previous == TASK_RUNNING && (next == TASK_READY || next == TASK_BLOCKED || next == TASK_DEAD)) ||
        (previous == TASK_BLOCKED && (next == TASK_READY || next == TASK_DEAD))
}

let taskGet(id: UWord): *mut Task {
    if id == 0 || id > MAX_TASKS return null
    return &mut tasks[id - 1]
}

// Only this operation publishes Ready, including creation and wakeup. All
// scheduler mutations require IE=0 or EXL=1, independent of PIC ENABLE.
let taskEnqueue(task: *mut Task): Void {
    if !taskIrqsDisabled() || task == &mut idleTask || task.queued || task.ipcEndpoint != null || readyCount == MAX_TASKS ||
        !taskTransitionAllowed(task.state, TASK_READY) {
        panic("invalid ready transition", null)
        return
    }
    readyQueue[(readyHead + readyCount) % MAX_TASKS] = task.id
    readyCount += 1
    task.waitReason = WAIT_NONE
    task.queued = true
    task.state = TASK_READY
}

// Release only inactive resources. Creation rollback uses the same ledger as
// the reaper; MMU teardown releases mapped frames, leaving unmapped entries.
let taskRollback(task: *mut Task): Void {
    if task.directory != null && !mmuDestroyAddressSpace(task.directory, task.id) {
        panic("could not roll back task directory", null)
        return
    }
    task.directory = null
    if physicalPageOwned(task.bootPage, task.id, PAGE_USER) &&
        !freePage(task.bootPage, task.id, PAGE_USER) {
        panic("could not roll back start block", null)
        return
    }
    task.bootPage = PAGE_NONE
    task.deviceRights = 0
    for i: UWord in 0..TASK_PAGE_COUNT {
        if physicalPageOwned(task.pages[i], task.id, taskPurposes[i]) &&
            !freePage(task.pages[i], task.id, taskPurposes[i]) {
            panic("could not roll back task page", null)
            return
        }
        task.pages[i] = PAGE_NONE
    }
    if task.kernelStackBottom != 0 && !mmuFreeKernelStack(task.kernelStackBottom, task.id) {
        panic("could not release task kernel stack", null)
        return
    }
    task.kernelStackBottom = 0
    task.kernelStackTop = 0
}

// A bounded lifetime table: Dead records remain for diagnostics, slots are
// not recycled yet. Failed creations leave an Empty slot and can be retried.
// Trusted init API, sealed by schedulerStarted. Only embedded, aligned text
// images are accepted. Allocation stays on the kernel-owned per-task ledger.
// Successful construction is NOT schedulable until resources are installed.
let taskCreateImage(sourceStart: UWord, sourceEnd: UWord, entryOffset: UWord): UWord {
    if !taskIrqsDisabled() || schedulerStarted || sourceStart < (&__start_text as UWord) ||
        sourceEnd > (&__stop_text as UWord) || sourceEnd <= sourceStart ||
        sourceStart % WORD_BYTES != 0 || sourceEnd % WORD_BYTES != 0 return 0
    let codeBytes: UWord = sourceEnd - sourceStart
    if codeBytes > PAGE_SIZE || entryOffset >= codeBytes || entryOffset % WORD_BYTES != 0 return 0
    let mut task: *mut Task = null
    for i: UWord in 0..MAX_TASKS {
        if tasks[i].state == TASK_EMPTY {
            task = &mut tasks[i]
            break
        }
    }
    if task == null return 0
    task.id = ((task as UWord) - (&tasks[0] as UWord)) / sizeof(Task) + 1
    task.asid = task.id // 1..8; full TLB flush on every activation, no leases
    task.directory = mmuCreateAddressSpace(task.id) as *mut UWord
    if task.directory == null return 0
    if !allocTaskPages(task.id, &taskPurposes[0], &mut task.pages[0], TASK_PAGE_COUNT) {
        taskRollback(task)
        return 0
    }
    task.kernelStackBottom = mmuAllocKernelStack(task.id)
    if task.kernelStackBottom == 0 {
        taskRollback(task)
        return 0
    }
    task.kernelStackTop = task.kernelStackBottom + KERNEL_STACK_BYTES
    let stack: *mut UWord = task.kernelStackBottom as *mut UWord
    stack[0] = STACK_CANARY
    // Copy before granting X: the shared kernel alias then becomes read-only.
    let source: *UWord = sourceStart as *UWord
    let code: *mut UWord = task.pages[0] as *mut UWord
    for i: UWord in 0..(codeBytes / WORD_BYTES) code[i] = source[i]
    if !mapPage(task.directory, task.id, USER_CODE, task.pages[0], PTE_RX | PTE_U) ||
        !mapPage(task.directory, task.id, USER_DATA, task.pages[1], PTE_RW | PTE_U) ||
        !mapPage(task.directory, task.id, USER_STACK_BOTTOM, task.pages[2], PTE_RW | PTE_U) {
        taskRollback(task)
        return 0
    }
    task.userCode = USER_CODE
    task.userData = USER_DATA
    task.userStackBottom = USER_STACK_BOTTOM
    task.userStackTop = USER_STACK_TOP
    task.ptbr = (task.directory as UWord) | (task.asid << 4) | PTBR_ENABLE
    // No TLS/crt0: tp, fp, ra and FCSR start at zero; user sp is aligned.
    for i: UWord in 0..GPR_COUNT task.context.regs[i] = 0
    task.context.regs[REG_SP] = USER_STACK_TOP
    task.context.epc = USER_CODE + entryOffset
    task.context.status = STATUS_EXL | STATUS_PUM // PIE=PSS=IE=UM=SS=0
    task.context.cause = 0
    task.context.badaddr = 0
    task.context.fcsr = 0
    task.context.ptbr = task.ptbr
    task.context.reserved[0] = 0
    task.context.reserved[1] = 0
    task.state = TASK_CREATED
    return task.id
}

// Retained for scheduler/CPU acceptance fixtures. Only trusted kernel init
// can load this demo and grant its diagnostic UART operation.
let taskCreate(): UWord {
    let id: UWord = taskCreateImage(&userCodeStart as UWord, &userCodeEnd as UWord, 0)
    if id == 0 return 0
    let task: *mut Task = taskGet(id)
    task.context.regs[1] = USER_DATA
    task.context.regs[2] = PAGE_SIZE
    task.context.regs[3] = id
    task.deviceRights = DEVICE_UART_TX
    taskEnqueue(task)
    return id
}

// Exact rights are checked against the task's own table before copying the
// start record into a private RO/NX page. User claims never grant authority.
let taskInstallStart(id: UWord, block: *TaskStart): Bool {
    if !taskIrqsDisabled() || schedulerStarted || block == null || !taskStartBlockValid(block) return false
    let task: *mut Task = taskGet(id)
    if task == null || task.state != TASK_CREATED || task.bootPage != PAGE_NONE || block.taskId != id return false
    let entry: *mut Handle = handleEntry(&mut task.handles, block.endpoint)
    let object: *mut Endpoint = handleLookup(&mut task.handles, block.endpoint, block.rights)
    if entry == null || entry.rights != block.rights || object == null || object.mode != ENDPOINT_SERVICE ||
        (block.role == START_ROLE_SERVER && object.manager != id) return false
    task.bootPage = allocPage(id, PAGE_USER)
    if task.bootPage == PAGE_NONE return false
    let destination: *mut TaskStart = task.bootPage as *mut TaskStart
    destination[0] = *block
    if !mapPage(task.directory, id, START_BLOCK_VA, task.bootPage, PTE_RO | PTE_U) {
        if !freePage(task.bootPage, id, PAGE_USER) panic("could not release start block", null)
        task.bootPage = PAGE_NONE
        return false
    }
    task.deviceRights = block.devices
    task.context.regs[1] = START_BLOCK_VA
    task.context.regs[2] = START_BLOCK_BYTES
    return true
}

let taskInstallServiceStart(id: UWord, block: *ServiceStart, diskIrq: UWord): Bool {
    if !taskIrqsDisabled() || schedulerStarted || !serviceStartValid(block) || block.taskId != id return false
    let task: *mut Task = taskGet(id)
    if task == null || task.state != TASK_CREATED || task.bootPage != PAGE_NONE return false
    let entry: *mut Handle = handleEntry(&mut task.handles, block.endpoint)
    let object: *mut Endpoint = handleLookup(&mut task.handles, block.endpoint, block.rights)
    if entry == null || entry.rights != block.rights || object == null || object.mode != ENDPOINT_SERVICE ||
        (block.role != 2 && object.manager != id) return false
    if block.role == START_ROLE_SERVER {
        let bitmap: *mut Handle = handleEntry(&mut task.handles, block.bitmapEndpoint)
        let storage: *mut Endpoint = handleLookup(&mut task.handles, block.bitmapEndpoint, RIGHT_SEND)
        if bitmap == null || bitmap.rights != RIGHT_SEND || storage == null || storage.mode != ENDPOINT_SERVICE ||
            !irqTokenValid(id, block.irq, VIDEO_IRQ) ||
            !mmuScreenResourcesValid(task.directory, id, block.fontBytes) return false
    } else if ((block.role == START_ROLE_STORAGE || block.role == START_ROLE_DISK) &&
        !irqTokenValid(id, block.irq, diskIrq)) return false
    else if block.role == START_ROLE_INPUT && !irqTokenValid(id, block.irq, KEYBOARD_IRQ) return false
    if block.role == START_ROLE_FILE || (block.role == START_ROLE_CLIENT && block.protocol == START_PROTOCOL_FILE) {
        let upstream: *mut Handle = handleEntry(&mut task.handles, block.bitmapEndpoint)
        let service: *mut Endpoint = handleLookup(&mut task.handles, block.bitmapEndpoint, RIGHT_SEND)
        if upstream == null || upstream.rights != RIGHT_SEND || service == null || service.mode != ENDPOINT_SERVICE return false
        let manager: *mut Task = taskGet(service.manager)
        let mut expected: UWord = DEVICE_DISK
        if block.role == START_ROLE_CLIENT expected = DEVICE_INPUT
        if manager == null || manager.deviceRights != expected || manager.bootPage == PAGE_NONE return false
        let upstreamStart: *ServiceStart = manager.bootPage as *ServiceStart
        if block.role == START_ROLE_FILE && upstreamStart.role != START_ROLE_DISK return false
        if block.role == START_ROLE_CLIENT {
            if upstreamStart.role != START_ROLE_INPUT return false
            let fileTask: *mut Task = taskGet(object.manager)
            if fileTask == null || fileTask.bootPage == PAGE_NONE return false
            let fileStart: *ServiceStart = fileTask.bootPage as *ServiceStart
            if fileStart.role != START_ROLE_FILE || fileStart.protocol != START_PROTOCOL_FILE return false
        }
    }
    task.bootPage = allocPage(id, PAGE_USER)
    if task.bootPage == PAGE_NONE return false
    let destination: *mut ServiceStart = task.bootPage as *mut ServiceStart
    destination[0] = *block
    if !mapPage(task.directory, id, START_BLOCK_VA, task.bootPage, PTE_RO | PTE_U) {
        if !freePage(task.bootPage, id, PAGE_USER) panic("could not release service start", null)
        task.bootPage = PAGE_NONE
        return false
    }
    task.deviceRights = block.devices
    task.context.regs[1] = START_BLOCK_VA
    task.context.regs[2] = SERVICE_START_BYTES
    return true
}

let taskPublish(id: UWord): Bool {
    if !taskIrqsDisabled() || schedulerStarted return false
    let task: *mut Task = taskGet(id)
    if task == null || task.state != TASK_CREATED || task.bootPage == PAGE_NONE return false
    taskEnqueue(task)
    return true
}

// Failure before publication revokes endpoints, restores W^X aliases and
// frees only this task's owned resources. Handle generations are preserved.
let taskDiscardCreated(id: UWord): Bool {
    if !taskIrqsDisabled() || schedulerStarted return false
    let task: *mut Task = taskGet(id)
    if task == null || task.state != TASK_CREATED || task.queued return false
    handlesReleaseTask(&mut task.handles, id)
    irqReleaseTask(id)
    fontCancelOwner(id)
    screenReleaseOwner(id)
    inputReleaseOwner(id)
    taskRollback(task)
    task.state = TASK_EMPTY
    return true
}

let taskInitAvailable(): Bool {
    if !taskIrqsDisabled() || schedulerStarted || readyCount != 0 return false
    for i: UWord in 0..MAX_TASKS {
        if tasks[i].state != TASK_EMPTY return false
    }
    return true
}

let taskPrepare(): Bool {
    if tasks[0].state != TASK_EMPTY || schedulerStarted return false
    return taskCreate() == 1
}

// Trusted boot policy: task 1 manages the endpoint, task 2 can only send.
// r4 carries a task-local handle, never a global endpoint ID or pointer.
let taskBootstrapEndpoints(): Bool {
    if !taskIrqsDisabled() || schedulerStarted ||
        tasks[0].state != TASK_READY || tasks[1].state != TASK_READY return false
    let manager: Word = endpointBootstrap(&mut tasks[0].handles, tasks[0].id)
    if manager < 0 return false
    let sender: Word = handleCopy(&mut tasks[0].handles, manager as UWord,
        &mut tasks[1].handles, tasks[0].id, tasks[1].id, RIGHT_SEND)
    if sender < 0 {
        if handleClose(&mut tasks[0].handles, manager as UWord) != 0 {
            panic("could not roll back bootstrap endpoint", null)
        }
        return false
    }
    tasks[0].context.regs[4] = manager as UWord
    tasks[1].context.regs[4] = sender as UWord
    return true
}

let taskSetStack(bottom: UWord, top: UWord): Void {
    let bottomSlot: *mut UWord = KERNEL_STACK_BOTTOM as *mut UWord
    let topSlot: *mut UWord = KERNEL_STACK_TOP as *mut UWord
    let spSlot: *mut UWord = KERNEL_SP as *mut UWord
    bottomSlot[0] = bottom
    topSlot[0] = top
    spSlot[0] = top
}

// Dispatch returns a trusted frame pointer. The old M call stack, the chosen
// stack, TCB frames and entry code share supervisor mappings in every root.
let taskSelect(): *TrapFrame {
    if !taskIrqsDisabled() || !schedulerStarted ||
        (currentTask != null && currentTask != &mut idleTask) {
        panic("invalid scheduler context", null)
        return null
    }
    if readyCount == 0 {
        if !mmuActivateKernel() || mfcr(CR_PTBR) != idleTask.ptbr {
            panic("could not restore kernel directory", null)
            return null
        }
        idleTask.state = TASK_RUNNING
        currentTask = &mut idleTask
        taskSetStack(idleTask.kernelStackBottom, idleTask.kernelStackTop)
        fence()
        return &idleTask.context
    }
    let task: *mut Task = taskGet(readyQueue[readyHead])
    if task == null || task.state != TASK_READY || !task.queued ||
        task.context.ptbr != task.ptbr ||
        !mmuSwitchAddressSpace(task.directory, task.id, task.asid) || mfcr(CR_PTBR) != task.ptbr {
        panic("invalid selected task", null)
        return null
    }
    readyHead = (readyHead + 1) % MAX_TASKS
    readyCount -= 1
    task.queued = false
    task.state = TASK_RUNNING
    idleTask.state = TASK_READY // dormant fallback, deliberately not queued
    currentTask = task
    // Initial frames acquire PIE only after the timer branch is ready.
    if timerReady task.context.status |= STATUS_PIE
    taskSetStack(task.kernelStackBottom, task.kernelStackTop)
    fence()
    return &task.context
}

let taskStart(clock: UWord): Void {
    let enabled: *volatile UWord = PIC_ENABLE as *volatile UWord
    if schedulerStarted || readyCount == 0 || mfcr(CR_STATUS) & STATUS_IE != 0 || *enabled != 0 {
        panic("invalid first task entry", null)
        return
    }
    idleTask.kernelStackBottom = mmuAllocKernelStack(IDLE_STACK_OWNER)
    if idleTask.kernelStackBottom == 0 {
        panic("could not allocate idle stack", null)
        return
    }
    idleTask.kernelStackTop = idleTask.kernelStackBottom + KERNEL_STACK_BYTES
    let stack: *mut UWord = idleTask.kernelStackBottom as *mut UWord
    stack[0] = STACK_CANARY
    idleTask.ptbr = mfcr(CR_PTBR)
    idleTask.directory = (idleTask.ptbr & ~(PAGE_SIZE - 1)) as *mut UWord
    idleTask.state = TASK_READY
    for i: UWord in 0..GPR_COUNT idleTask.context.regs[i] = 0
    idleTask.context.regs[REG_SP] = idleTask.kernelStackTop
    idleTask.context.epc = &taskKernelResume as UWord
    // IRET enters supervisor with IE=EXL=0; idle keeps IE off through WFI.
    idleTask.context.status = STATUS_EXL
    idleTask.context.cause = 0
    idleTask.context.badaddr = 0
    idleTask.context.fcsr = 0
    idleTask.context.ptbr = idleTask.ptbr
    idleTask.context.reserved[0] = 0
    idleTask.context.reserved[1] = 0
    endpointSealBootstrap()
    mmuSealResources()
    irqSeal()
    schedulerStarted = true
    if !timerInit(clock, SCHEDULER_QUANTUM_HZ) {
        panic("invalid scheduler timer configuration", null)
        return
    }
    trapRestoreFrame(taskSelect())
}

// Called on the idle stack with IE=EXL=UM=0. Single-CPU wakeup handlers
// cannot publish Ready between this check and the assembly WFI.
let taskIdlePoll(): *TrapFrame {
    if !schedulerStarted || currentTask != &mut idleTask || idleTask.state != TASK_RUNNING ||
        mfcr(CR_STATUS) & (STATUS_IE | STATUS_EXL | STATUS_UM) != 0 ||
        mfcr(CR_PTBR) != idleTask.ptbr {
        panic("invalid idle context", null)
        return null
    }
    if readyCount != 0 return taskSelect()
    if !timerCanSleep() {
        panic("idle without wakeup IRQ", null)
        return null
    }
    return null
}

let taskOwnsTrap(frame: *TrapFrame): Bool {
    return currentTask != null && currentTask != &mut idleTask && currentTask.state == TASK_RUNNING &&
        !currentTask.queued && frame.status & STATUS_PUM != 0 &&
        frame.ptbr == currentTask.ptbr && mfcr(CR_PTBR) == frame.ptbr
}

let taskSaveContext(frame: *TrapFrame): Void {
    if taskOwnsTrap(frame) currentTask.context = *frame
}

// The caller finalizes the frame; timer IRQs leave EPC/results unchanged.
let taskYield(frame: *TrapFrame): *TrapFrame {
    if !taskIrqsDisabled() || !taskOwnsTrap(frame) {
        panic("yield without running task", frame)
        return null
    }
    taskSaveContext(frame)
    taskEnqueue(currentTask)
    currentTask = null
    return taskSelect()
}

// Timer acknowledgement precedes this call. IRQ EPC is never advanced.
let taskTick(frame: *TrapFrame): *TrapFrame {
    if !taskIrqsDisabled() || !schedulerStarted || !timerReady {
        panic("timer without scheduler", frame)
        return frame
    }
    if frame.status & STATUS_PUM != 0 return taskYield(frame)
    // Only idle runs with supervisor IRQs enabled. Kernel work stays atomic.
    if currentTask != &mut idleTask || idleTask.state != TASK_RUNNING || frame.ptbr != idleTask.ptbr {
        panic("timer outside supervisor idle", frame)
        return frame
    }
    if readyCount == 0 return frame
    return taskSelect()
}

// A device wake can schedule from idle immediately without rotating a user
// task or pretending that this IRQ is a timer quantum.
let taskIrqReturn(frame: *TrapFrame): *TrapFrame {
    if currentTask == &mut idleTask && readyCount != 0 return taskSelect()
    return frame
}

// Kernel-only blocking primitive; IPC publishes its pinned wait before this call.
let taskBlock(frame: *TrapFrame, reason: UWord): *TrapFrame {
    if !taskIrqsDisabled() || !taskOwnsTrap(frame) || reason == WAIT_NONE {
        panic("invalid task wait", frame)
        return null
    }
    if (((reason >= WAIT_IPC_SEND && reason <= WAIT_IPC_REPLY) &&
        (currentTask.ipcEndpoint == null || currentTask.ipcKind != reason)) ||
        (currentTask.ipcEndpoint != null && currentTask.ipcKind != reason)) {
        panic("invalid IPC block", frame)
        return null
    }
    taskSaveContext(frame)
    currentTask.waitReason = reason
    currentTask.state = TASK_BLOCKED
    currentTask = null
    return taskSelect()
}

let taskWake(id: UWord): Bool {
    // Wakeup may originate in supervisor idle or a future device handler.
    // Recheck state under the same exclusion as block/finish/selection.
    let status: UWord = memoryLock()
    let task: *mut Task = taskGet(id)
    if task == null || task.state != TASK_BLOCKED || task.ipcEndpoint != null {
        memoryUnlock(status)
        return false
    }
    taskEnqueue(task)
    memoryUnlock(status)
    return true
}

let taskFinish(frame: *TrapFrame, code: Word, faulted: Bool): *TrapFrame {
    if !taskIrqsDisabled() || !taskOwnsTrap(frame) {
        panic("user trap without running task", frame)
        return null
    }
    taskSaveContext(frame)
    currentTask.exitCode = code
    currentTask.faulted = faulted
    handlesReleaseTask(&mut currentTask.handles, currentTask.id)
    irqReleaseTask(currentTask.id)
    fontCancelOwner(currentTask.id)
    screenReleaseOwner(currentTask.id)
    inputReleaseOwner(currentTask.id)
    currentTask.deviceRights = 0
    currentTask.waitReason = WAIT_NONE
    currentTask.state = TASK_DEAD
    currentTask = null
    return taskSelect()
}

// Kernel-only termination of a suspended task. Its stack/root are inactive;
// the normal reaper runs later on the current task's selected stack.
let taskAbortBlocked(id: UWord, code: Word, faulted: Bool): Bool {
    if !taskIrqsDisabled() return false
    let task: *mut Task = taskGet(id)
    if task == null || task == currentTask || task.state != TASK_BLOCKED || task.queued return false
    ipcCancelTask(id)
    task.exitCode = code
    task.faulted = faulted
    task.waitReason = WAIT_NONE
    task.state = TASK_DEAD
    handlesReleaseTask(&mut task.handles, id)
    irqReleaseTask(id)
    fontCancelOwner(id)
    screenReleaseOwner(id)
    inputReleaseOwner(id)
    task.deviceRights = 0
    return true
}

// Assembly has ALREADY moved sp onto the selected kernel stack. Never run
// this from taskFinish: its M frames still occupy the retiring task's stack.
// Terminal TCB contexts remain intact after resources are released.
let taskReap(): Void {
    if !schedulerStarted || !taskIrqsDisabled() {
        panic("invalid task cleanup context", null)
        return
    }
    let bottomSlot: *UWord = KERNEL_STACK_BOTTOM as *UWord
    let topSlot: *UWord = KERNEL_STACK_TOP as *UWord
    let sp: UWord = taskKernelSp()
    if sp <= *bottomSlot || sp > *topSlot ||
        currentTask == null || mfcr(CR_PTBR) != currentTask.ptbr {
        panic("invalid task cleanup stack", null)
        return
    }
    fontReap()
    for i: UWord in 0..MAX_TASKS {
        let task: *mut Task = &mut tasks[i]
        if task.state != TASK_DEAD || task.reaped continue
        if task == currentTask || task.queued ||
            (sp >= task.kernelStackBottom && sp <= task.kernelStackTop) {
            panic("task resources still in use", null)
            return
        }
        if !serviceDevicesQuiescent(task.id) continue
        taskRollback(task)
        task.reaped = true
        debugPrint("LA/IX: task $u stopped, state=$u code=$i cause=$u epc=$h\n",
            task.id, task.state, task.exitCode, task.context.cause, task.context.epc)
    }
}

export { Task, tasks, idleTask, currentTask, MAX_TASKS, TASK_EMPTY, TASK_READY, TASK_RUNNING,
    TASK_BLOCKED, TASK_DEAD, TASK_CREATED, WAIT_NONE, WAIT_EVENT, WAIT_IPC_SEND, WAIT_IPC_RECEIVE,
    WAIT_IPC_CALL, WAIT_IPC_ACCEPT, WAIT_IPC_REPLY, WAIT_IRQ,
    USER_CODE, USER_DATA, USER_STACK_BOTTOM, USER_STACK_TOP, USER_STACK_GUARD,
    taskPrepare, taskCreate, taskGet, taskStart, taskBootstrapEndpoints, taskIdlePoll, taskTransitionAllowed,
    taskCreateImage, taskInstallStart, taskPublish, taskDiscardCreated, taskInitAvailable,
    taskInstallServiceStart, taskIrqReturn,
    taskSaveContext, taskOwnsTrap, taskYield, taskTick, taskBlock, taskWake, taskFinish, taskAbortBlocked, taskReap }

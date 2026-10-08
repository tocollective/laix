import { memoryRevokeLoader, memoryRevokeTask, memoryReapRegions, memoryReapOrphans } from "../mm/runtime.m"
import { memoryBudgetOpen, memoryBudgetClose } from "../mm/memory.m"
// Preemptive round-robin: one CPU, one thread per task, no nested traps.
import { PAGE_SIZE, WORD_BYTES, GPR_COUNT, REG_SP, STATUS_IE, STATUS_PIE, STATUS_PUM,
    STATUS_EXL, STATUS_UM, CR_STATUS, CR_PTBR, PTBR_ENABLE, PTE_U, PTE_RX, PTE_RW, PTE_RO,
    STACK_CANARY, KERNEL_STACK_BYTES, KERNEL_SP, KERNEL_STACK_BOTTOM,
    KERNEL_STACK_TOP, PIC_ENABLE } from "../arch/wrm081632/defs.m"
import { taskRecordCompletion, taskRecordReaped, taskReleaseSupervisor, taskControlSeal } from "control.m"
import { PTE_X, PTE_W, PAGE_MASK, STACK_ALIGNMENT } from "../arch/wrm081632/defs.m"
import { TrapFrame } from "../trap/trap_frame.m"
import { PAGE_NONE, PAGE_USER, PAGE_USER_STACK, allocPage, allocTaskPages, freePage,
    physicalPageOwned, memoryLock, memoryUnlock } from "../mm/memory.m"
import { USER_VA_START, USER_VA_END, mmuCreateAddressSpace, mmuDestroyAddressSpace,
    mmuSwitchAddressSpace, mmuActivateKernel, mapPage, mmuUserLeaf,
    mmuAllocKernelStack, mmuFreeKernelStack } from "../mm/mmu.m"
import { mmuSealResources, mmuResourcesValid } from "../mm/mmu.m"
import { irqReleaseTask, irqSeal, irqTokenValid } from "../drivers/irq.m"
import { DEVICE_ROLE_SCREEN, DEVICE_ROLE_INPUT, deviceRoleIrq, deviceRoleBlobBytes } from "../drivers/device_table.m"
import { deviceCancelOwner, deviceReap, screenReleaseOwner, serviceDevicesQuiescent } from "../drivers/service_devices.m"
import { inputReleaseOwner } from "../drivers/input_device.m"
import { netReleaseOwner } from "../drivers/net_device.m"
import { ServiceStart, serviceStartValid } from "service_start.m"
import { panic } from "../kernel/panic.m"
import { debugPrint } from "../drivers/debug_uart.m"
import { TimerCount, timerReady, timerInit, timerCanSleep } from "../drivers/timer.m"
import { Handle, HandleTable, handlesReleaseTask, endpointBootstrap, handleCopy,
    handleClose, endpointSealBootstrap, handleEntry, handleLookup, ENDPOINT_SERVICE } from "../ipc/objects.m"
import { TaskStart, taskStartBlockValid } from "start.m"
import { ipcCancelTask } from "../ipc/ipc.m"
import { Endpoint } from "../ipc/objects.m"
import { RIGHT_SEND, IPC_MESSAGE_MAX, START_BLOCK_VA, START_BLOCK_BYTES,
    START_ROLE_SERVER, START_ROLE_STORAGE, DEVICE_UART_TX,
    SERVICE_START_BYTES, START_ROLE_INPUT, START_ROLE_DISK,
    START_ROLE_FILE, START_ROLE_CLIENT, START_PROTOCOL_FILE, DEVICE_INPUT, DEVICE_DISK,
    LIFETIME_REPLY_RESERVE, TASK_SLOTS, TASK_SLOT_BITS, TASK_SLOT_MASK, TASK_GENERATION_MAX } from "../arch/wrm081632/defs.m"

let SCHEDULER_QUANTUM_HZ: UWord = 100
let TASK_RECOVERY_RESERVE: UWord = 2
// Dead address spaces torn down per IRQ-excluded reaping section (G5). One root
// is at most 1024 directory entries plus eight 1024-leaf tables.
let TASK_REAP_STAGE_TASKS: UWord = 1
// taskIdlePoll result meaning "a reap stage ran and more remain": the idle
// loop opens one IRQ window and polls again instead of sleeping. Never a frame.
let TASK_IDLE_STAGE: UWord = 1
let IDLE_STACK_OWNER: UWord = TASK_SLOTS + 1 // slot bits zero: never a task budget
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
let WAIT_SLEEP: UWord = 8
let USER_CODE: UWord = USER_VA_START
let USER_DATA: UWord = USER_CODE + PAGE_SIZE
let USER_STACK_TOP: UWord = USER_VA_END
let USER_STACK_BOTTOM: UWord = USER_STACK_TOP - PAGE_SIZE
let USER_STACK_GUARD: UWord = USER_STACK_BOTTOM - PAGE_SIZE
let TASK_PAGE_COUNT: UWord = 3

type Task {
    id: UWord, // generation-bearing reference, including resource ownership
    slot: UWord, // diagnostic index only; never authority
    createImages: UWord, // nontransferable bootstrap creation capability (image mask)
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
    context: TrapFrame, // keep offset and array stride eight-byte aligned
    deviceFactory: UWord, // nontransferable bounded broker operation mask
    state: UWord,
    queued: Bool,
    waitReason: UWord,
    exitCode: Word,
    faulted: Bool,
    reaped: Bool,
    reusable: Bool, // runtime construction only; legacy boot diagnostics remain
    configured: Bool,
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
    waitTimed: UWord, // authoritative deadline flag; cleared before Ready/death
    waitDeadline: TimerCount,
    resolverOwner: UWord, // authorized private supervisor, full reference
    resolverMask: UWord, // service IDs explicitly permitted before publication
    waitPadding: UWord, // keep the array stride aligned to eight bytes
    ipcPadding: UWord, // keep every TCB's TrapFrame aligned to eight bytes
}

// The task table and the ready ring have one entry per slot this boot supports.
// They are carved out of RAM at boot (src/task/tables.m), so the number of slots
// follows the installed memory instead of a compile-time constant. Slots are
// handed out lowest first; taskHighWater is the highest one ever handed out and
// bounds every scan, since a record beyond it is still all zero.
let mut tasks: *mut Task
let mut taskCapacity: UWord
let mut taskHighWater: UWord
let mut currentTask: *mut Task
let mut readyQueue: *mut UWord // generation-bearing references, never user pointers; taskCapacity entries
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
    return (previous == TASK_EMPTY && next == TASK_CREATED) ||
        (previous == TASK_CREATED && (next == TASK_READY || next == TASK_EMPTY)) ||
        (previous == TASK_READY && (next == TASK_RUNNING || next == TASK_DEAD)) ||
        (previous == TASK_RUNNING && (next == TASK_READY || next == TASK_BLOCKED || next == TASK_DEAD)) ||
        (previous == TASK_BLOCKED && (next == TASK_READY || next == TASK_DEAD)) ||
        (previous == TASK_DEAD && next == TASK_EMPTY)
}

// Hands the carved tables to the scheduler, once. Both are zero-filled by the allocator.
let taskTableBind(taskBase: UWord, readyBase: UWord, capacity: UWord): Bool {
    if tasks != null || taskBase == 0 || readyBase == 0 || capacity < 2 || capacity > TASK_SLOTS return false
    tasks = taskBase as *mut Task
    readyQueue = readyBase as *mut UWord
    taskCapacity = capacity
    return true
}

// Slot lookup is kernel-internal enumeration, never user task resolution.
let taskSlot(slot: UWord): *mut Task {
    if slot == 0 || slot > taskCapacity return null
    return &mut tasks[slot - 1]
}

let taskGet(id: UWord): *mut Task {
    let task: *mut Task = taskSlot(id & TASK_SLOT_MASK)
    if task == null || task.id != id || task.state == TASK_EMPTY return null
    return task
}

// Only this operation publishes Ready, including creation and wakeup. All
// scheduler mutations require IE=0 or EXL=1, independent of PIC ENABLE.
let taskEnqueue(task: *mut Task): Void {
    if !taskIrqsDisabled() || task == &mut idleTask || task.state == TASK_EMPTY ||
        (task.state == TASK_CREATED && task.reusable && !task.configured) || task.queued || task.ipcEndpoint != null || readyCount == taskCapacity ||
        !taskTransitionAllowed(task.state, TASK_READY) {
        panic("invalid ready transition", null)
        return
    }
    readyQueue[(readyHead + readyCount) % taskCapacity] = task.id
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
    memoryReapRegions(task.id)
    if physicalPageOwned(task.bootPage, task.id, PAGE_USER) &&
        !freePage(task.bootPage, task.id, PAGE_USER) {
        panic("could not roll back start block", null)
        return
    }
    task.bootPage = PAGE_NONE
    task.deviceRights = 0
    task.deviceFactory = 0
    task.createImages = 0
    task.resolverOwner = 0
    task.resolverMask = 0
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
    if !memoryBudgetClose(task.id) panic("task budget remains charged", null)
}

// Remaining admissions of a counter that never wraps. Zero means retired.
let lifetimeLeft(generation: UWord, limit: UWord): UWord {
    if generation >= limit return 0
    return limit - generation
}

// Shared checked mechanism: trusted pointers only, no implicit authority.
// Boot and runtime policy select approved ranges before reaching this entry.
let taskConstructImage(sourceStart: UWord, sourceEnd: UWord, entryOffset: UWord): UWord {
    if !taskIrqsDisabled() || sourceStart < (&__start_text as UWord) ||
        sourceEnd > (&__stop_text as UWord) || sourceEnd <= sourceStart ||
        sourceStart % WORD_BYTES != 0 || sourceEnd % WORD_BYTES != 0 return 0
    let codeBytes: UWord = sourceEnd - sourceStart
    if codeBytes > PAGE_SIZE || entryOffset >= codeBytes || entryOffset % WORD_BYTES != 0 return 0
    let mut task: *mut Task = null
    // The first pass leaves namespaces within the lifetime reserve of their
    // limit for last, so replacing a client near the limit lands in a fresh
    // namespace instead of the same one. The second pass is the old rule.
    for pass: UWord in 0..2 {
        for i: UWord in 0..taskCapacity {
            // Bootstrap and the sealed recovery policy may use the final two slots.
            if currentTask != null && !currentTask.handles.factoryRecovery && i >= taskCapacity - TASK_RECOVERY_RESERVE continue
            // Retired reply namespaces require replacement in a different slot.
            // Construction and collection must never reset the reply counter.
            if tasks[i].state == TASK_EMPTY && tasks[i].id >> TASK_SLOT_BITS < TASK_GENERATION_MAX &&
                tasks[i].ipcCallGeneration < TASK_GENERATION_MAX {
                if pass == 0 && lifetimeLeft(tasks[i].ipcCallGeneration, TASK_GENERATION_MAX) <= LIFETIME_REPLY_RESERVE continue
                task = &mut tasks[i]
                break
            }
        }
        if task != null break
    }
    if task == null return 0
    task.slot = ((task as UWord) - (&tasks[0] as UWord)) / sizeof(Task) + 1
    if task.slot > taskHighWater taskHighWater = task.slot
    // Generation zero is the first boot lifetime. Every later reservation,
    // including failed construction, advances; exhausted slots never wrap.
    let mut generation: UWord = task.id >> TASK_SLOT_BITS
    if task.id != 0 generation += 1
    task.id = (generation << TASK_SLOT_BITS) | task.slot
    task.asid = task.slot & 255 // full TLB flush on activation, no ASID leases
    task.state = TASK_CREATED
    task.reaped = false
    task.reusable = false
    task.configured = false
    task.deviceFactory = 0
    task.createImages = 0
    task.resolverOwner = 0
    task.resolverMask = 0
    task.exitCode = 0
    task.faulted = false
    task.waitReason = WAIT_NONE
    task.ipcKind = WAIT_NONE
    task.bootPage = PAGE_NONE
    for i: UWord in 0..TASK_PAGE_COUNT task.pages[i] = PAGE_NONE
    if !memoryBudgetOpen(task.id) {
        task.state = TASK_EMPTY
        return 0
    }
    task.directory = mmuCreateAddressSpace(task.id) as *mut UWord
    if task.directory == null {
        if !memoryBudgetClose(task.id) panic("empty budget remains charged", null)
        task.state = TASK_EMPTY
        return 0
    }
    if !allocTaskPages(task.id, &taskPurposes[0], &mut task.pages[0], TASK_PAGE_COUNT) {
        taskRollback(task)
        task.state = TASK_EMPTY
        return 0
    }
    task.kernelStackBottom = mmuAllocKernelStack(task.id)
    if task.kernelStackBottom == 0 {
        taskRollback(task)
        task.state = TASK_EMPTY
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
        task.state = TASK_EMPTY
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

// Bootstrap policy remains sealed independently of the shared mechanism.
let taskCreateImage(sourceStart: UWord, sourceEnd: UWord, entryOffset: UWord): UWord {
    if schedulerStarted return 0
    return taskConstructImage(sourceStart, sourceEnd, entryOffset)
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
    task.configured = true
    return true
}

// Checked service start record. At boot the record also grants the device
// rights it names; at run time (a supervisor starting its own child) the child
// must already hold exactly those rights from the device broker.
let taskServiceStartInstall(id: UWord, block: *ServiceStart, diskIrq: UWord, runtime: Bool): Bool {
    if !taskIrqsDisabled() || !serviceStartValid(block) || block.taskId != id return false
    let task: *mut Task = taskGet(id)
    if task == null || task.state != TASK_CREATED || task.bootPage != PAGE_NONE ||
        (runtime && task.deviceRights != block.devices) return false
    let entry: *mut Handle = handleEntry(&mut task.handles, block.endpoint)
    let object: *mut Endpoint = handleLookup(&mut task.handles, block.endpoint, block.rights)
    if entry == null || entry.rights != block.rights || object == null || object.mode != ENDPOINT_SERVICE ||
        (block.role != 2 && object.manager != id) return false
    if block.role == START_ROLE_SERVER {
        let bitmap: *mut Handle = handleEntry(&mut task.handles, block.bitmapEndpoint)
        let storage: *mut Endpoint = handleLookup(&mut task.handles, block.bitmapEndpoint, RIGHT_SEND)
        if bitmap == null || bitmap.rights != RIGHT_SEND || storage == null || storage.mode != ENDPOINT_SERVICE ||
            !irqTokenValid(id, block.irq, deviceRoleIrq(DEVICE_ROLE_SCREEN)) ||
            block.fontBytes != deviceRoleBlobBytes(DEVICE_ROLE_SCREEN) ||
            !mmuResourcesValid(task.directory, id, DEVICE_ROLE_SCREEN) return false
    } else if ((block.role == START_ROLE_STORAGE || block.role == START_ROLE_DISK) &&
        !irqTokenValid(id, block.irq, diskIrq)) return false
    else if block.role == START_ROLE_INPUT && !irqTokenValid(id, block.irq, deviceRoleIrq(DEVICE_ROLE_INPUT)) return false
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
    task.configured = true
    return true
}

let taskInstallServiceStart(id: UWord, block: *ServiceStart, diskIrq: UWord): Bool {
    if schedulerStarted return false
    return taskServiceStartInstall(id, block, diskIrq, false)
}

let taskPublishChecked(id: UWord): Bool {
    if !taskIrqsDisabled() return false
    let task: *mut Task = taskGet(id)
    if task == null || task.state != TASK_CREATED || !task.configured || task.bootPage == PAGE_NONE ||
        task.queued || task.ipcEndpoint != null || task.context.ptbr != task.ptbr ||
        task.context.status != (STATUS_EXL | STATUS_PUM) || task.context.epc % WORD_BYTES != 0 ||
        task.context.regs[1] != START_BLOCK_VA ||
        task.context.regs[REG_SP] != task.userStackTop || task.userStackTop % STACK_ALIGNMENT != 0 return false
    let code: UWord = mmuUserLeaf(task.directory, id, task.context.epc & ~PAGE_MASK)
    let stack: UWord = mmuUserLeaf(task.directory, id, task.userStackBottom)
    let start: UWord = mmuUserLeaf(task.directory, id, START_BLOCK_VA)
    if code & (PTE_U | PTE_X | PTE_W) != (PTE_U | PTE_X) ||
        stack & (PTE_U | PTE_W | PTE_X) != (PTE_U | PTE_W) ||
        start & ~PAGE_MASK != task.bootPage || start & (PTE_U | PTE_W | PTE_X) != PTE_U return false
    memoryRevokeLoader(id)
    taskEnqueue(task)
    return true
}

let taskPublish(id: UWord): Bool {
    if schedulerStarted return false
    return taskPublishChecked(id)
}

// Failure before publication revokes endpoints, restores W^X aliases and
// frees only this task's owned resources. Handle generations are preserved.
let taskDiscardChecked(id: UWord): Bool {
    if !taskIrqsDisabled() return false
    let task: *mut Task = taskGet(id)
    if task == null || task.state != TASK_CREATED || task.queued return false
    handlesReleaseTask(&mut task.handles, id)
    irqReleaseTask(id)
    deviceCancelOwner(id)
    screenReleaseOwner(id)
    inputReleaseOwner(id)
    netReleaseOwner(id)
    taskRollback(task)
    task.state = TASK_EMPTY
    return true
}

let taskDiscardCreated(id: UWord): Bool {
    if schedulerStarted return false
    return taskDiscardChecked(id)
}

let taskBootConstructionOpen(): Bool { return !schedulerStarted }

let taskInitAvailable(): Bool {
    if !taskIrqsDisabled() || schedulerStarted || readyCount != 0 return false
    for i: UWord in 0..taskHighWater {
        if tasks[i].state != TASK_EMPTY return false
    }
    return true
}

let taskPrepare(): Bool {
    if tasks[0].state != TASK_EMPTY || schedulerStarted return false
    return taskCreate() != 0
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
    readyHead = (readyHead + 1) % taskCapacity
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
    taskControlSeal()
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
// cannot publish Ready between this check and the assembly WFI. Returns a
// frame to dispatch, null to sleep, or TASK_IDLE_STAGE to poll again after an
// IRQ window (staged reaping has more roots to tear down).
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
    // Timer/device IRQs may return to this same idle frame. Reap on the safe
    // idle stack even when no context switch follows a late DMA completion.
    if taskReap() return TASK_IDLE_STAGE as *TrapFrame
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
    if task == null || task.state != TASK_BLOCKED || task.ipcEndpoint != null || task.waitTimed != 0 {
        memoryUnlock(status)
        return false
    }
    taskEnqueue(task)
    memoryUnlock(status)
    return true
}

let taskRemoveReady(task: *mut Task): Void {
    let mut position: UWord = 0
    while position < readyCount && readyQueue[(readyHead + position) % taskCapacity] != task.id position += 1
    if !task.queued || position == readyCount {
        panic("missing ready task", null)
        return
    }
    while position + 1 < readyCount {
        readyQueue[(readyHead + position) % taskCapacity] = readyQueue[(readyHead + position + 1) % taskCapacity]
        position += 1
    }
    readyQueue[(readyHead + readyCount - 1) % taskCapacity] = 0
    readyCount -= 1
    task.queued = false
}

// Logical death never frees the stack/root. Detach waits before revoking
// handles so cancellation cannot wake or queue the terminating task.
let taskStop(task: *mut Task, code: Word, faulted: Bool, terminated: Bool): Void {
    if task.queued taskRemoveReady(task)
    ipcCancelTask(task.id)
    task.exitCode = code
    task.faulted = faulted
    task.waitReason = WAIT_NONE
    task.state = TASK_DEAD
    task.deviceFactory = 0
    task.createImages = 0
    task.resolverOwner = 0
    task.resolverMask = 0
    memoryRevokeTask(task.id)
    handlesReleaseTask(&mut task.handles, task.id)
    irqReleaseTask(task.id)
    deviceCancelOwner(task.id)
    screenReleaseOwner(task.id)
    inputReleaseOwner(task.id)
    netReleaseOwner(task.id)
    task.deviceRights = 0
    taskRecordCompletion(task, terminated)
    taskReleaseSupervisor(task.id)
}

let taskFinish(frame: *TrapFrame, code: Word, faulted: Bool): *TrapFrame {
    if !taskIrqsDisabled() || !taskOwnsTrap(frame) {
        panic("user trap without running task", frame)
        return null
    }
    taskSaveContext(frame)
    taskStop(currentTask, code, faulted, false)
    currentTask = null
    return taskSelect()
}

// Authorized syscall code resolves a scoped control capability before this
// mechanism. On one CPU a Running target is necessarily the calling task.
let taskTerminateChecked(frame: *TrapFrame, id: UWord, code: Word): *TrapFrame {
    let task: *mut Task = taskGet(id)
    if !taskIrqsDisabled() || task == null ||
        (task.state != TASK_READY && task.state != TASK_RUNNING && task.state != TASK_BLOCKED) return frame
    if task == currentTask {
        taskSaveContext(frame)
        taskStop(task, code, false, true)
        currentTask = null
        return taskSelect()
    }
    taskStop(task, code, false, true)
    return frame
}

// Kernel-only fixture helper, using the same cancellation and death path.
let taskAbortBlocked(id: UWord, code: Word, faulted: Bool): Bool {
    if !taskIrqsDisabled() return false
    let task: *mut Task = taskGet(id)
    if task == null || task == currentTask || task.state != TASK_BLOCKED || task.queued return false
    taskStop(task, code, faulted, true)
    return true
}

// Assembly has ALREADY moved sp onto the selected kernel stack. Never run
// this from taskFinish: its M frames still occupy the retiring task's stack.
// Runtime slots become Empty only after physical reclamation. Boot fixture
// contexts remain intact; bounded diagnostic history is independent of reuse.
//
// Teardown is staged (G5): one call commits at most TASK_REAP_STAGE_TASKS roots,
// so the section is bounded by one address space, not by the number of tasks. A
// dead task keeps its root, frames, stack and charges until its own stage
// commits `reaped`; nothing is partly released between stages. Returns true when
// another reapable task waits, and the caller resumes at its next IRQ window
// (idle loop) or trap return. A task blocked on a device (not quiescent) neither
// spends the stage nor keeps the idle loop awake.
let taskReap(): Bool {
    if !schedulerStarted || !taskIrqsDisabled() {
        panic("invalid task cleanup context", null)
        return false
    }
    let bottomSlot: *UWord = KERNEL_STACK_BOTTOM as *UWord
    let topSlot: *UWord = KERNEL_STACK_TOP as *UWord
    let sp: UWord = taskKernelSp()
    if sp <= *bottomSlot || sp > *topSlot ||
        currentTask == null || mfcr(CR_PTBR) != currentTask.ptbr {
        panic("invalid task cleanup stack", null)
        return false
    }
    deviceReap()
    memoryReapOrphans()
    let mut staged: UWord = 0
    for i: UWord in 0..taskHighWater {
        let task: *mut Task = &mut tasks[i]
        if task.state != TASK_DEAD || task.reaped continue
        if task == currentTask || task.queued ||
            (sp >= task.kernelStackBottom && sp <= task.kernelStackTop) {
            panic("task resources still in use", null)
            return false
        }
        if !serviceDevicesQuiescent(task.id) continue
        if staged == TASK_REAP_STAGE_TASKS return true
        taskRollback(task)
        task.reaped = true
        taskRecordReaped(task.id)
        debugPrint("LA/IX: task $u stopped, state=$u code=$i cause=$u epc=$h\n",
            task.id, task.state, task.exitCode, task.context.cause, task.context.epc)
        if task.reusable task.state = TASK_EMPTY
        staged += 1
    }
    return false
}

export { Task, tasks, idleTask, currentTask, taskCapacity, taskHighWater, taskTableBind, TASK_EMPTY, TASK_READY, TASK_RUNNING,
    TASK_BLOCKED, TASK_DEAD, TASK_CREATED, WAIT_NONE, WAIT_EVENT, WAIT_IPC_SEND, WAIT_IPC_RECEIVE,
    WAIT_IPC_CALL, WAIT_IPC_ACCEPT, WAIT_IPC_REPLY, WAIT_IRQ, WAIT_SLEEP,
    USER_CODE, USER_DATA, USER_STACK_BOTTOM, USER_STACK_TOP, USER_STACK_GUARD,
    taskPrepare, taskCreate, taskGet, taskStart, taskBootstrapEndpoints, taskIdlePoll, taskTransitionAllowed,
    taskCreateImage, taskInstallStart, taskPublish, taskDiscardCreated, taskInitAvailable, taskBootConstructionOpen,
    taskInstallServiceStart, taskServiceStartInstall, taskIrqReturn, taskSlot, TASK_RECOVERY_RESERVE, lifetimeLeft,
    taskConstructImage, taskPublishChecked, taskDiscardChecked, taskTerminateChecked,
    taskSaveContext, taskOwnsTrap, taskYield, taskTick, taskBlock, taskWake, taskFinish, taskAbortBlocked, taskReap }

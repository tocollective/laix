import { taskConstructProgram, taskConstructBuffer, taskProgramValid } from "program.m"
import { serviceReleaseSupervisor, serviceDependencyLive } from "recovery.m"
import { irqIssue, irqReleaseTask, irqRetiredCount } from "../drivers/irq.m"
import { diskDevicesCheck, diskDevicesRegrant, serviceDiskIrq, deviceExtentConfigure,
    screenDevicesCheck, screenDevicesRegrant } from "../drivers/service_devices.m"
import { kernelBootInfo } from "../kernel/boot.m"
import { inputDevicesInit } from "../drivers/input_device.m"
import { netDevicesInit } from "../drivers/net_device.m"
import { DEVICE_UART_TX, DEVICE_INPUT, DEVICE_DISK, DEVICE_FONT, DEVICE_SCREEN, DEVICE_NET,
    START_HANDLES_MAGIC, START_HANDLES_MAX, WORD_BYTES, TASK_SLOT_BITS, TASK_GENERATION_MAX, START_MAGIC, SERVICE_START_VERSION,
    SERVICE_START_BYTES, START_DATA_VA, IPC_MESSAGE_MAX, START_ROLE_SERVER, START_ROLE_CLIENT,
    SCREEN_FONT_VA, RIGHT_SEND, RIGHT_RECEIVE } from "../arch/wrm081632/defs.m"
import { DEVICE_ROLE_INPUT, DEVICE_ROLE_SCREEN, DEVICE_ROLE_NET, deviceRoleIrq,
    deviceRoleBlobBytes } from "../drivers/device_table.m"
// Bounded, nontransferable task capabilities and completion mailboxes.
// References select records; only the kernel-recorded owner grants authority.
import { Task, currentTask, taskGet, taskConstructImage, taskPublishChecked,
    taskDiscardChecked, taskTerminateChecked, taskSaveContext, taskOwnsTrap,
    taskServiceStartInstall, tasks, taskCapacity, taskHighWater, TASK_RECOVERY_RESERVE, lifetimeLeft,
    TASK_CREATED, TASK_DEAD, USER_DATA } from "task.m"
import { RuntimeStart, TaskEvent, LifetimeReport } from "runtime_start.m"
import { ServiceStart } from "service_start.m"
import { TrapFrame } from "../trap/trap_frame.m"
import { PAGE_NONE, PAGE_USER, PAGE_KERNEL, allocPage, allocPageRun, freePage,
    memoryFreePages, MEMORY_RESERVE_PAGES } from "../mm/memory.m"
import { mapPage, copyToUser, copyFromUser } from "../mm/mmu.m"
import { handleCopy, handleLookup, handleClose } from "../ipc/objects.m"
import { objectAssertAtomic, endpointRetiredCount, MAX_HANDLES, HANDLE_GENERATION_MAX } from "../ipc/objects.m"
import { serviceDevicesQuiescent } from "../drivers/service_devices.m"
import { panic } from "../kernel/panic.m"
import { TASK_RIGHT_CONFIGURE, TASK_RIGHT_PUBLISH, TASK_RIGHT_INSPECT,
    TASK_RIGHT_TERMINATE, TASK_RIGHT_COLLECT, TASK_RIGHT_ALL,
    TASK_EVENT_FAULT, TASK_EVENT_TERMINATED, TASK_EVENT_RECLAIMED,
    TASK_EVENT_QUARANTINED, RUNTIME_START_MAGIC, RUNTIME_START_VERSION,
    RUNTIME_START_BYTES, START_BLOCK_VA, PAGE_SIZE, PAGE_MASK, PTE_RO, PTE_U,
    RIGHT_ALL, ERRNO_EPERM, ERRNO_ESRCH, ERRNO_EINVAL, ERRNO_ENFILE,
    ERRNO_EAGAIN, ERRNO_EBUSY, TASK_LOAD_BYTES, IMAGE_LOAD_AUTHORITY } from "../arch/wrm081632/defs.m"

let TASK_DOMAIN_QUOTA: UWord = 4 // includes uncollected child completion records
let TASK_CONTROL_RESERVE: UWord = 2
let TASK_HISTORY_SIZE: UWord = 32 // diagnostic ring; never used as authority

type TaskControl {
    owner: UWord, // supervisor reference, not its diagnostic slot
    reference: UWord,
    rights: UWord,
    done: Bool,
    event: TaskEvent,
}
// One row per possible child, so also the bound on uncollected completion events.
// Carved out of RAM at boot (src/task/tables.m). Rows are used lowest first and
// taskControlHigh is one past the highest row ever used: every scan stops there.
let mut taskControls: *mut TaskControl
let mut taskControlCount: UWord
let mut taskControlHigh: UWord
let taskControlTableBind(base: UWord, count: UWord): Bool {
    if taskControls != null || base == 0 || count < 2 return false
    taskControls = base as *mut TaskControl
    taskControlCount = count
    return true
}
let mut taskHistory: TaskEvent[TASK_HISTORY_SIZE]
let mut taskHistoryHead: UWord
let mut taskControlsSealed: Bool
let mut taskHistoryCount: UWord

extern let runtimeApprovedStart: UByte
extern let runtimeApprovedEnd: UByte

// Immutable boot catalog: registration closes with task-control sealing. Image 1
// is the kernel's approved self-test fixture; IDs 2.. are build-issued ELF rows
// (ImageRow) loaded in order. The kernel owns no list of image names.
let IMAGE_CATALOG_MAX: UWord = 31 // IDs 1..31; bit 31 of Task.createImages is IMAGE_LOAD_AUTHORITY
type ImageRow {
    start: UWord,
    end: UWord,
}
let mut runtimeImageStart: UWord[IMAGE_CATALOG_MAX]
let mut runtimeImageEnd: UWord[IMAGE_CATALOG_MAX]
let taskImageRowValid(image: UWord, start: UWord, end: UWord): Bool {
    return !taskControlsSealed && image >= 2 && image <= IMAGE_CATALOG_MAX && start != 0 &&
        end > start && runtimeImageStart[image - 1] == 0
}
let taskRegisterImage(image: UWord, start: UWord, end: UWord): Bool {
    objectAssertAtomic()
    if !taskImageRowValid(image, start, end) return false
    runtimeImageStart[image - 1] = start
    runtimeImageEnd[image - 1] = end
    return true
}
// Registers rows as images 2..count+1 all-or-nothing and returns the creation
// mask covering image 1 and the loaded rows, or zero when any row is rejected.
let taskCatalogLoad(rows: *ImageRow, count: UWord): UWord {
    objectAssertAtomic()
    if rows == null || count == 0 || count >= IMAGE_CATALOG_MAX return 0
    for i: UWord in 0..count {
        if !taskImageRowValid(i + 2, rows[i].start, rows[i].end) return 0
    }
    for i: UWord in 0..count {
        runtimeImageStart[i + 1] = rows[i].start
        runtimeImageEnd[i + 1] = rows[i].end
    }
    return (1 << (count + 1)) - 1
}

let taskControlLookup(reference: UWord, rights: UWord): *mut TaskControl {
    objectAssertAtomic()
    if currentTask == null || currentTask.id == 0 || rights == 0 return null
    for i: UWord in 0..taskControlHigh {
        let control: *mut TaskControl = &mut taskControls[i]
        if control.reference == reference && reference != 0 && control.owner == currentTask.id &&
            control.rights & rights == rights return control
    }
    return null
}

// The first unused row, extending the used range by one when there is none.
// Taking a row is the caller's decision: the range grows only when it does.
let taskControlFree(): *mut TaskControl {
    for i: UWord in 0..taskControlHigh {
        if taskControls[i].reference == 0 return &mut taskControls[i]
    }
    if taskControlHigh < taskControlCount return &mut taskControls[taskControlHigh]
    return null
}

let taskControlTake(control: *mut TaskControl): Void {
    let index: UWord = ((control as UWord) - (&taskControls[0] as UWord)) / sizeof(TaskControl)
    if index >= taskControlHigh taskControlHigh = index + 1
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
    for i: UWord in 0..taskControlHigh {
        if taskControls[i].reference == reference return false
    }
    taskControlTake(control)
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

// Reserves the next completion row for the caller, or null when its child quota
// or the table is full. The row is taken before any fallible allocation: no task
// can exit without completion storage, even when the supervisor is slow.
let taskRuntimeReserve(): *mut TaskControl {
    let mut charged: UWord = 0
    let mut control: *mut TaskControl = null
    for i: UWord in 0..taskControlHigh {
        let row: *mut TaskControl = &mut taskControls[i]
        if row.owner == currentTask.id && row.reference != 0 && row.reference != currentTask.id charged += 1
        if !currentTask.handles.factoryRecovery && i >= taskControlCount - TASK_CONTROL_RESERVE continue
        if control == null && row.reference == 0 control = row
    }
    if control == null && taskControlHigh < taskControlCount &&
        (currentTask.handles.factoryRecovery || taskControlHigh < taskControlCount - TASK_CONTROL_RESERVE) {
        control = &mut taskControls[taskControlHigh]
    }
    if ((!currentTask.handles.factoryRecovery && charged >= TASK_DOMAIN_QUOTA) || control == null) return null
    taskControlTake(control)
    control.owner = currentTask.id
    control.rights = TASK_RIGHT_ALL
    control.done = false
    return control
}

// Shared tail of catalog creation and image loading: the constructed child is
// recorded in the reserved row and owned by the caller.
let taskRuntimeAdopt(control: *mut TaskControl, reference: UWord): Word {
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

// Image IDs are catalog choices, never addresses. Creation authority is an
// image mask on the caller; the new per-object capability controls only child.
let taskRuntimeCreate(image: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || image == 0 || image > IMAGE_CATALOG_MAX ||
        currentTask.createImages & (1 << (image - 1)) == 0 ||
        (image != 1 && runtimeImageStart[image - 1] == 0) return -ERRNO_EPERM
    let control: *mut TaskControl = taskRuntimeReserve()
    if control == null return -ERRNO_ENFILE
    let mut reference: UWord = 0
    if image == 1 reference = taskConstructImage(&runtimeApprovedStart as UWord, &runtimeApprovedEnd as UWord, 0)
    else reference = taskConstructProgram(runtimeImageStart[image - 1], runtimeImageEnd[image - 1])
    return taskRuntimeAdopt(control, reference)
}

// Reserved kernel identity (beside the DMA bounce and MMU owners): it owns the
// snapshot frames of one SYS_TASK_LOAD call and nothing else, and never outlives it.
let LOAD_STAGE_OWNER: UWord = 0xFFFFFFFD

// With the snapshot frames in `stage`: copy the caller's image, check it, and
// build the child. Everything the call allocated here is released on failure.
let taskRuntimeLoadStaged(stage: UWord, source: UWord, length: UWord): Word {
    let copied: Word = copyFromUser(currentTask.directory, currentTask.id, stage as *mut UByte, source, length)
    if copied != 0 return copied
    if !taskProgramValid(stage, stage + length) return -ERRNO_EINVAL
    let control: *mut TaskControl = taskRuntimeReserve()
    if control == null return -ERRNO_ENFILE
    return taskRuntimeAdopt(control, taskConstructBuffer(stage, stage + length))
}

// Loads an ELF image from the caller's memory instead of the catalog. It needs
// the nontransferable IMAGE_LOAD_AUTHORITY bit, spends the same child quota and
// completion row as a catalog creation, applies the catalog's image checks and
// resource limits, and returns a Created child with the same control rights.
// The kernel works on a private snapshot held in contiguous free frames for the
// duration of this call only: the syscall runs with IRQs excluded on one CPU, so
// the user cannot change the bytes between the checks and their use, and no
// static kernel memory is spent on it. The 16-frame progress reserve is kept.
// EINVAL: length out of range or not an acceptable image. EFAULT: unreadable
// buffer. ENFILE: no frames for the snapshot, or quota, rows or construction
// resources exhausted (all rolled back).
let taskRuntimeLoad(source: UWord, length: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || currentTask.id == 0 || currentTask.createImages & IMAGE_LOAD_AUTHORITY == 0 return -ERRNO_EPERM
    if length < 52 || length > TASK_LOAD_BYTES return -ERRNO_EINVAL
    let pages: UWord = (length + PAGE_MASK) / PAGE_SIZE
    if memoryFreePages < pages + MEMORY_RESERVE_PAGES return -ERRNO_ENFILE
    let stage: UWord = allocPageRun(LOAD_STAGE_OWNER, PAGE_KERNEL, pages)
    if stage == PAGE_NONE return -ERRNO_ENFILE
    let result: Word = taskRuntimeLoadStaged(stage, source, length)
    for page: UWord in 0..pages {
        if !freePage(stage + page * PAGE_SIZE, LOAD_STAGE_OWNER, PAGE_KERNEL) panic("could not release load snapshot", null)
    }
    return result
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

// Scoped brokers: UART TX, raw keyboard batches, approved read-only extents
// (generic Disk or the font-bitmap reader), the display and the Ethernet card. The display grant
// installs exactly the Screen role's device-table rows into the unpublished
// child; a manager picks no address, size or permission. Display policy lives in
// user mode. Disk, bitmap storage and display are each issued alone.
let taskRuntimeDevices(reference: UWord, devices: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || devices == 0 ||
        devices & ~(DEVICE_UART_TX | DEVICE_INPUT | DEVICE_DISK | DEVICE_FONT | DEVICE_SCREEN | DEVICE_NET) != 0 ||
        (devices & (DEVICE_DISK | DEVICE_FONT | DEVICE_SCREEN | DEVICE_NET) != 0 &&
            devices != DEVICE_DISK && devices != DEVICE_FONT && devices != DEVICE_SCREEN &&
            devices != DEVICE_NET) ||
        currentTask.deviceFactory & devices != devices return -ERRNO_EPERM
    if taskControlLookup(reference, TASK_RIGHT_CONFIGURE) == null return -ERRNO_EPERM
    let child: *mut Task = taskGet(reference)
    if child == null || child.state != TASK_CREATED || child.configured ||
        child.deviceRights != 0 return -ERRNO_EBUSY
    let mut token: UWord = 0
    if devices & DEVICE_INPUT != 0 {
        token = irqIssue(reference, deviceRoleIrq(DEVICE_ROLE_INPUT))
        if token == 0 return -ERRNO_EBUSY
        if !inputDevicesInit(reference, token) {
            irqReleaseTask(reference)
            return -ERRNO_EBUSY
        }
    }
    if devices == DEVICE_SCREEN {
        let available: Word = screenDevicesCheck()
        if available != 0 return available
        token = irqIssue(reference, deviceRoleIrq(DEVICE_ROLE_SCREEN))
        if token == 0 return -ERRNO_EBUSY
        let result: Word = screenDevicesRegrant(reference)
        if result != 0 {
            irqReleaseTask(reference)
            return result
        }
    }
    if devices == DEVICE_NET {
        token = irqIssue(reference, deviceRoleIrq(DEVICE_ROLE_NET))
        if token == 0 return -ERRNO_EBUSY
        if !netDevicesInit(reference, token) {
            irqReleaseTask(reference)
            return -ERRNO_EBUSY
        }
    }
    if devices == DEVICE_DISK || devices == DEVICE_FONT {
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

// A manager may select a subextent of its approved boot resource only for
// its own unpublished child. Possession of the child reference is insufficient.
let taskRuntimeExtent(reference: UWord, offset: UWord, bytes: UWord, flags: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || currentTask.deviceFactory & DEVICE_DISK == 0 ||
        taskControlLookup(reference, TASK_RIGHT_CONFIGURE) == null return -ERRNO_EPERM
    let child: *mut Task = taskGet(reference)
    if child == null || child.deviceRights != DEVICE_DISK return -ERRNO_EPERM
    return deviceExtentConfigure(reference, offset, bytes, flags)
}

// Start handle list of an unpublished child the caller controls: the words after
// the magic and count in the child's data page (user/starthandles.m). Entries are
// {token, rights} pairs in the caller's memory. With rights, the token names one
// of the caller's own handles and the child gets an attenuated copy, which is the
// same copy rule as the one start endpoint of SYS_TASK_CONFIGURE; with rights 0
// the token is stored as a plain word, such as the interrupt token returned by
// SYS_TASK_DEVICES, which only its issuing owner can use. Every entry is checked
// before anything is installed, and a failed copy closes the earlier copies.
// One call at a time (the kernel is non-preemptible), so a static scratch row is
// enough. It is filled bytewise (the user pointer need not be aligned) and read
// back as little-endian words.
let mut taskHandleBytes: UByte[48]
let taskHandleWord(index: UWord): UWord {
    return (taskHandleBytes[4 * index] as UWord) | ((taskHandleBytes[4 * index + 1] as UWord) << 8) |
        ((taskHandleBytes[4 * index + 2] as UWord) << 16) | ((taskHandleBytes[4 * index + 3] as UWord) << 24)
}
let taskRuntimeHandles(reference: UWord, entries: UWord, count: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || currentTask.id == 0 return -ERRNO_EPERM
    if count == 0 || count > START_HANDLES_MAX return -ERRNO_EINVAL
    if taskControlLookup(reference, TASK_RIGHT_CONFIGURE) == null return -ERRNO_EPERM
    let child: *mut Task = taskGet(reference)
    if child == null || child.state != TASK_CREATED return -ERRNO_EBUSY
    let page: *mut UWord = child.pages[1] as *mut UWord
    if page[0] == START_HANDLES_MAGIC return -ERRNO_EBUSY
    let copied: Word = copyFromUser(currentTask.directory, currentTask.id,
        &mut taskHandleBytes[0], entries, count * 2 * WORD_BYTES)
    if copied != 0 return copied
    for i: UWord in 0..count {
        let rights: UWord = taskHandleWord(2 * i + 1)
        if rights != 0 && (rights & ~RIGHT_ALL != 0 ||
            handleLookup(&mut currentTask.handles, taskHandleWord(2 * i), rights) == null) return -ERRNO_EPERM
    }
    let mut installed: UWord[6]
    for i: UWord in 0..count {
        installed[i] = taskHandleWord(2 * i)
        if taskHandleWord(2 * i + 1) == 0 continue
        let result: Word = handleCopy(&mut currentTask.handles, taskHandleWord(2 * i), &mut child.handles,
            currentTask.id, reference, taskHandleWord(2 * i + 1))
        if result < 0 {
            for j: UWord in 0..i {
                if taskHandleWord(2 * j + 1) != 0 && handleClose(&mut child.handles, installed[j]) != 0 panic("could not roll back start handle", null)
            }
            return result
        }
        installed[i] = result as UWord
    }
    page[0] = START_HANDLES_MAGIC
    page[1] = count
    for i: UWord in 0..START_HANDLES_MAX {
        if i < count page[2 + i] = installed[i]
        else page[2 + i] = 0
    }
    return count as Word
}

// The checked service start record (user/services) for an unpublished child the
// caller controls, in place of the boot-time policy that built it. The caller
// names the child's role and protocol and passes its own handles: `endpoint` is
// copied into the child (receive, or send for a client) and `upstream`, when
// nonzero, as the one send handle the role needs on the service below it. The
// device rights and the interrupt token must already come from SYS_TASK_DEVICES;
// the record is the same one the boot policy would have installed, so every
// cross-check of taskServiceStartInstall (role, rights, upstream manager, token)
// applies unchanged. A failure closes the copies and leaves the child Created.
let taskRuntimeServiceStart(reference: UWord, role: UWord, protocol: UWord, endpoint: UWord,
    upstream: UWord, irq: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || currentTask.id == 0 || taskControlLookup(reference, TASK_RIGHT_CONFIGURE) == null return -ERRNO_EPERM
    let child: *mut Task = taskGet(reference)
    if child == null || child.state != TASK_CREATED || child.configured || child.bootPage != PAGE_NONE return -ERRNO_EBUSY
    let mut rights: UWord = RIGHT_RECEIVE
    if role == START_ROLE_CLIENT rights = RIGHT_SEND
    if endpoint == 0 || handleLookup(&mut currentTask.handles, endpoint, rights) == null return -ERRNO_EPERM
    if upstream != 0 && handleLookup(&mut currentTask.handles, upstream, RIGHT_SEND) == null return -ERRNO_EPERM
    let mut block: ServiceStart
    block.magic = START_MAGIC
    block.version = SERVICE_START_VERSION
    block.bytes = SERVICE_START_BYTES
    block.role = role
    block.taskId = reference
    block.rights = rights
    block.devices = child.deviceRights
    block.data = START_DATA_VA
    block.dataBytes = PAGE_SIZE
    block.ipcLimit = IPC_MESSAGE_MAX
    block.protocol = protocol
    block.bitmapEndpoint = 0
    block.fontIndex = 0
    block.fontBytes = 0
    block.irq = irq
    if role == START_ROLE_SERVER {
        block.fontIndex = SCREEN_FONT_VA
        block.fontBytes = deviceRoleBlobBytes(DEVICE_ROLE_SCREEN)
    }
    let own: Word = handleCopy(&mut currentTask.handles, endpoint, &mut child.handles,
        currentTask.id, reference, rights)
    if own < 0 return own
    block.endpoint = own as UWord
    if upstream != 0 {
        let below: Word = handleCopy(&mut currentTask.handles, upstream, &mut child.handles,
            currentTask.id, reference, RIGHT_SEND)
        if below < 0 {
            if handleClose(&mut child.handles, own as UWord) != 0 panic("could not roll back service endpoint", null)
            return below
        }
        block.bitmapEndpoint = below as UWord
    }
    if !taskServiceStartInstall(reference, &block, serviceDiskIrq(kernelBootInfo.disk), true) {
        if handleClose(&mut child.handles, own as UWord) != 0 ||
            (upstream != 0 && handleClose(&mut child.handles, block.bitmapEndpoint) != 0) panic("could not roll back service handles", null)
        return -ERRNO_EINVAL
    }
    return 0
}

// Narrowing delegation of image authority to an unpublished child the caller
// controls: only bits the caller holds itself, and only before publication, so a
// loader such as Exec gets the load bit without any task ever gaining authority
// that its supervisor lacks.
let taskRuntimeAuthority(reference: UWord, images: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || currentTask.id == 0 || taskControlLookup(reference, TASK_RIGHT_CONFIGURE) == null return -ERRNO_EPERM
    if images == 0 return -ERRNO_EINVAL
    if images & ~currentTask.createImages != 0 return -ERRNO_EPERM
    let child: *mut Task = taskGet(reference)
    if child == null || child.state != TASK_CREATED return -ERRNO_EBUSY
    child.createImages = child.createImages | images
    return 0
}

let taskRuntimePublish(reference: UWord): Word {
    let control: *mut TaskControl = taskControlLookup(reference, TASK_RIGHT_PUBLISH)
    if control == null return -ERRNO_EPERM
    let task: *mut Task = taskGet(reference)
    if task == null || task.state != TASK_CREATED || !task.configured return -ERRNO_EBUSY
    let block: *RuntimeStart = task.bootPage as *RuntimeStart
    if block.magic == START_MAGIC {
        // The checked service record (SYS_TASK_SERVICE_START) lays its fields out differently.
        let service: *ServiceStart = task.bootPage as *ServiceStart
        if handleLookup(&mut task.handles, service.endpoint, service.rights) == null return -ERRNO_EPERM
    } else if block.endpoint != 0 && handleLookup(&mut task.handles, block.endpoint, block.rights) == null return -ERRNO_EPERM
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
    for i: UWord in 0..taskControlHigh {
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
    for i: UWord in 0..taskControlHigh {
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
    for i: UWord in 0..taskControlHigh {
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

// Read-only report for a creating supervisor; no counter changes. The caller
// needs creation authority. A nonzero reference also needs INSPECT on that
// child (until collection) and selects its reply namespace, task slot and
// handle table. A never-used slot still owns generation zero.
let taskRuntimeLifetime(reference: UWord, destination: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || currentTask.id == 0 || currentTask.createImages == 0 return -ERRNO_EPERM
    let mut selected: *mut Task = null
    if reference != 0 {
        if taskControlLookup(reference, TASK_RIGHT_INSPECT) == null return -ERRNO_EPERM
        selected = taskGet(reference)
        if selected == null return -ERRNO_ESRCH
    }
    let mut report: LifetimeReport
    report.bytes = sizeof(LifetimeReport)
    report.limit = TASK_GENERATION_MAX
    report.replySelected = 0
    report.replyTotal = 0
    report.replyOpen = 0
    report.taskSelected = 0
    report.handleSelected = 0
    report.retiredTasks = 0
    report.retiredHandles = 0
    // Slots beyond the high-water mark were never used: each is a full namespace.
    let mut ordinary: UWord = taskCapacity
    if !currentTask.handles.factoryRecovery ordinary = taskCapacity - TASK_RECOVERY_RESERVE
    if ordinary > taskHighWater {
        report.replyOpen = ordinary - taskHighWater
        report.replyTotal = (ordinary - taskHighWater) * TASK_GENERATION_MAX
    }
    for i: UWord in 0..taskHighWater {
        let task: *mut Task = &mut tasks[i]
        let replies: UWord = lifetimeLeft(task.ipcCallGeneration, TASK_GENERATION_MAX)
        let mut references: UWord = TASK_GENERATION_MAX + 1
        if task.id != 0 references = lifetimeLeft(task.id >> TASK_SLOT_BITS, TASK_GENERATION_MAX)
        for j: UWord in 0..MAX_HANDLES {
            if task.handles.entries[j].generation >= HANDLE_GENERATION_MAX report.retiredHandles += 1
        }
        if replies == 0 || references == 0 {
            report.retiredTasks += 1
            continue
        }
        // The same two slots the constructor withholds from ordinary callers.
        if !currentTask.handles.factoryRecovery && i >= taskCapacity - TASK_RECOVERY_RESERVE continue
        report.replyOpen += 1
        report.replyTotal += replies
    }
    if selected != null {
        report.replySelected = lifetimeLeft(selected.ipcCallGeneration, TASK_GENERATION_MAX)
        report.taskSelected = lifetimeLeft(selected.id >> TASK_SLOT_BITS, TASK_GENERATION_MAX)
        report.handleSelected = HANDLE_GENERATION_MAX
        for j: UWord in 0..MAX_HANDLES {
            let left: UWord = lifetimeLeft(selected.handles.entries[j].generation, HANDLE_GENERATION_MAX)
            if left < report.handleSelected report.handleSelected = left
        }
    }
    report.retiredEndpoints = endpointRetiredCount()
    report.retiredIrqs = irqRetiredCount()
    return copyToUser(currentTask.directory, currentTask.id, destination,
        &report as *UByte, sizeof(LifetimeReport))
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
    taskControlCount, taskControlHigh, taskControlTableBind, TASK_HISTORY_SIZE, taskControlBootstrapSelf, taskControlBootstrap, taskControlSeal, taskInstallRuntimeStart,
    taskControlLookupBootOpen, taskRegisterImage, taskCatalogLoad, ImageRow, IMAGE_CATALOG_MAX, taskRuntimeDiscard, taskRuntimeDevices, taskRuntimeExtent, taskRuntimeCreate, taskRuntimeLoad, taskRuntimeConfigure, taskRuntimeHandles, taskRuntimeServiceStart, taskRuntimeAuthority, taskRuntimePublish, taskRuntimeRead,
    taskRuntimeTerminate, taskRuntimeLifetime, taskRecordCompletion, taskRecordReaped, taskReleaseSupervisor }

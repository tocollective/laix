// Receiver-selected, single-use endpoint transfer. All records are kernel-owned.
import { Task, currentTask, taskGet, MAX_TASKS, TASK_READY,
    TASK_RUNNING, TASK_BLOCKED, taskSaveContext, taskOwnsTrap } from "../task/task.m"
import { Handle, MAX_HANDLES, HANDLE_GENERATION_MAX, ENDPOINT_RECOVERY_RESERVE,
    handleCopyCheck, handleLookup, handleInstallAt, objectAssertAtomic } from "objects.m"
import { ipcCallerValid } from "ipc.m"
import { TimerCount, timerDeadline, timerReadCount, timerDeadlineReached } from "../drivers/timer.m"
import { TrapFrame } from "../trap/trap_frame.m"
import { RIGHT_ALL, RIGHT_MANAGE, ERRNO_EPERM, ERRNO_ESRCH, ERRNO_EINVAL,
    ERRNO_EBADF, ERRNO_EMFILE, ERRNO_EBUSY, ERRNO_EAGAIN, ERRNO_EOVERFLOW } from "../arch/wrm081632/defs.m"

let TRANSFER_RESERVED: UWord = 1
let TRANSFER_DELIVERED: UWord = 2
type Transfer {
    generation: UWord, // survives task-slot reuse; retires before wrap
    state: UWord,
    receiver: UWord, // exact task lifetime
    sender: UWord, // exact authorized task lifetime
    slot: UWord,
    rights: UWord, // exact agreed attenuation, not just a maximum
    handle: UWord,
    deadline: TimerCount,
}
let mut transfers: Transfer[MAX_TASKS] // one reservation or notification per domain

let transferLive(task: *mut Task): Bool {
    return task != null && (task.state == TASK_READY || task.state == TASK_RUNNING || task.state == TASK_BLOCKED)
}

let transferClear(record: *mut Transfer): Void {
    if record.state == TRANSFER_RESERVED {
        let receiver: *mut Task = taskGet(record.receiver)
        if receiver != null receiver.handles.entries[record.slot - 1].reserved = false
    }
    record.state = 0
    record.receiver = 0
    record.sender = 0
    record.slot = 0
    record.rights = 0
    record.handle = 0
    record.deadline.lo = 0
    record.deadline.hi = 0
}

let transferExpire(record: *mut Transfer): Void {
    if record.state != TRANSFER_RESERVED return
    let mut now: TimerCount
    timerReadCount(&mut now)
    if timerDeadlineReached(&now, &record.deadline) transferClear(record)
}

// Reserve exactly one free ordinary handle slot; no source reference is pinned.
let transferReserve(slot: UWord, sender: UWord, rights: UWord, seconds: UWord): Word {
    if !ipcCallerValid() return -ERRNO_EPERM
    if !transferLive(taskGet(sender)) return -ERRNO_ESRCH
    if slot == 0 || slot > MAX_HANDLES || rights == 0 || rights & ~RIGHT_ALL != 0 return -ERRNO_EINVAL
    if sender != currentTask.id && rights & RIGHT_MANAGE != 0 return -ERRNO_EPERM
    if currentTask.handles.factoryRecovery && slot > MAX_HANDLES - ENDPOINT_RECOVERY_RESERVE return -ERRNO_EPERM
    let record: *mut Transfer = &mut transfers[currentTask.slot - 1]
    transferExpire(record)
    if record.state != 0 return -ERRNO_EBUSY
    if record.generation == HANDLE_GENERATION_MAX return -ERRNO_EOVERFLOW
    let entry: *mut Handle = &mut currentTask.handles.entries[slot - 1]
    if entry.object != null || entry.reserved || entry.generation == HANDLE_GENERATION_MAX return -ERRNO_EMFILE
    let mut deadline: TimerCount
    if !timerDeadline(seconds, &mut deadline) return -ERRNO_EINVAL
    record.generation += 1
    record.state = TRANSFER_RESERVED
    record.receiver = currentTask.id
    record.sender = sender
    record.slot = slot
    record.rights = rights
    record.deadline = deadline
    entry.reserved = true
    return ((record.generation << 8) | currentTask.slot) as Word
}

// Validation precedes installation and reference charging. A rejected attempt
// leaves consent available for retry; expiry/death/cancel explicitly releases it.
let transferCommit(source: UWord, receiverId: UWord, ticket: UWord, rights: UWord): Word {
    if !ipcCallerValid() return -ERRNO_EPERM
    let receiver: *mut Task = taskGet(receiverId)
    if !transferLive(receiver) return -ERRNO_ESRCH
    let record: *mut Transfer = &mut transfers[receiver.slot - 1]
    transferExpire(record)
    if ticket >> 8 == 0 || ticket >> 8 > HANDLE_GENERATION_MAX || ticket & 255 != receiver.slot ||
        record.generation != ticket >> 8 || record.state != TRANSFER_RESERVED ||
        record.receiver != receiverId || record.sender != currentTask.id return -ERRNO_EBADF
    if rights != record.rights return -ERRNO_EPERM
    let checked: Word = handleCopyCheck(&mut currentTask.handles, source, currentTask.id, receiverId, rights)
    if checked != 0 return checked
    if !receiver.handles.entries[record.slot - 1].reserved return -ERRNO_EBADF
    let installed: Word = handleInstallAt(&mut receiver.handles,
        handleLookup(&mut currentTask.handles, source, rights), rights, record.slot)
    if installed < 0 return installed
    receiver.handles.entries[record.slot - 1].reserved = false
    // Installation and authenticated notification publish in the same IRQ region.
    record.handle = installed as UWord
    record.state = TRANSFER_DELIVERED
    return 0
}

let transferCancel(ticket: UWord): Word {
    if !ipcCallerValid() return -ERRNO_EPERM
    let record: *mut Transfer = &mut transfers[currentTask.slot - 1]
    transferExpire(record)
    if record.receiver != currentTask.id || record.state != TRANSFER_RESERVED ||
        ticket != ((record.generation << 8) | currentTask.slot) return -ERRNO_EBADF
    transferClear(record)
    return 0
}

// Pull notification: r1=installed handle, r2=authenticated sender, r3=rights.
// No user-memory writes can fail after installation; consuming metadata never
// closes the installed handle. Pending/error outcomes clear identity outputs.
let transferCollect(frame: *mut TrapFrame, ticket: UWord): *TrapFrame {
    let mut result: Word = -ERRNO_EPERM
    let mut sender: UWord = 0
    let mut rights: UWord = 0
    if ipcCallerValid() && taskOwnsTrap(frame) {
        let record: *mut Transfer = &mut transfers[currentTask.slot - 1]
        transferExpire(record)
        result = -ERRNO_EBADF
        if record.receiver == currentTask.id && record.state != 0 &&
            ticket == ((record.generation << 8) | currentTask.slot) {
            result = -ERRNO_EAGAIN
            if record.state == TRANSFER_DELIVERED {
                result = record.handle as Word
                sender = record.sender
                rights = record.rights
                transferClear(record)
            }
        }
    }
    frame.regs[1] = result as UWord
    frame.regs[2] = sender
    frame.regs[3] = rights
    taskSaveContext(frame)
    return frame
}

let transferReleaseTask(owner: UWord): Void {
    objectAssertAtomic()
    for i: UWord in 0..MAX_TASKS {
        let record: *mut Transfer = &mut transfers[i]
        if record.receiver == owner || (record.state == TRANSFER_RESERVED && record.sender == owner) transferClear(record)
    }
}

let transferTimerTick(): Void {
    objectAssertAtomic()
    for i: UWord in 0..MAX_TASKS transferExpire(&mut transfers[i])
}
export { transferReserve, transferCommit, transferCancel, transferCollect,
    transferReleaseTask, transferTimerTick }

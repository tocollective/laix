// Kernel-owned level IRQ subscriptions. One CPU; callers run under EXL or
// with IE clear. Devices remain masked until their owner explicitly rearms.
import { PIC_LINE_COUNT, PIC_PENDING, PIC_ENABLE, TIMER_IRQ, VIDEO_IRQ, KEYBOARD_IRQ,
    ERRNO_EPERM, ERRNO_EINVAL, ERRNO_EBUSY, ERRNO_ETIMEDOUT, REG_RESULT } from "../arch/wrm081632/defs.m"
import { Task, taskGet, currentTask, TASK_CREATED, TASK_BLOCKED, WAIT_IRQ,
    taskBlock, taskWake, taskSaveContext } from "../task/task.m"
import { TrapFrame } from "../trap/trap_frame.m"
import { objectAssertAtomic } from "../ipc/objects.m"
import { panic } from "../kernel/panic.m"
import { TimerCount, timerDeadline, timerReadCount, timerDeadlineReached } from "timer.m"

type IrqGrant {
    owner: UWord,
    generation: UWord,
    pending: Bool,
    inService: Bool,
    waiting: Bool,
    timed: Bool,
    deadline: TimerCount,
}
let mut irqGrants: IrqGrant[PIC_LINE_COUNT]
let mut irqSealed: Bool

let irqMask(line: UWord): Void {
    let enabled: *volatile mut UWord = PIC_ENABLE as *volatile mut UWord
    enabled[0] &= ~(1 as UWord << line)
    fence()
}

// Boot may grant keyboard, video or the selected disk line, never the timer.
let irqGrant(owner: UWord, line: UWord): UWord {
    objectAssertAtomic()
    let task: *mut Task = taskGet(owner)
    if irqSealed || task == null || task.state != TASK_CREATED ||
        (line != KEYBOARD_IRQ && line != VIDEO_IRQ && line != 3 && line != 4 && line != 6) return 0
    let grant: *mut IrqGrant = &mut irqGrants[line]
    if grant.owner != 0 || grant.generation == 0x7FFFFF return 0
    irqMask(line)
    grant.owner = owner
    grant.generation += 1
    grant.pending = false
    grant.inService = true // initial arm also requires cleared device state
    grant.waiting = false
    grant.timed = false
    return grant.generation << 8 | (line + 1)
}

let irqSeal(): Void { irqSealed = true }

let irqLookup(owner: UWord, token: UWord): UWord {
    let slot: UWord = token & 255
    if owner == 0 || slot == 0 || slot > PIC_LINE_COUNT return PIC_LINE_COUNT
    let grant: *IrqGrant = &irqGrants[slot - 1]
    if grant.owner != owner || grant.generation != token >> 8 return PIC_LINE_COUNT
    return slot - 1
}

let irqTokenValid(owner: UWord, token: UWord, line: UWord): Bool {
    return line < PIC_LINE_COUNT && irqLookup(owner, token) == line
}

let irqNotify(line: UWord): Bool {
    objectAssertAtomic()
    if line >= PIC_LINE_COUNT || line == TIMER_IRQ return false
    irqMask(line) // even an unowned or unserviced level cannot storm on IRET
    let grant: *mut IrqGrant = &mut irqGrants[line]
    if grant.owner == 0 return false
    // One notification per arm. A still-asserted level must not recreate a
    // notification already consumed by the blocked wait or a previous wait.
    if grant.inService return true
    grant.pending = true
    grant.inService = true
    grant.timed = false
    let task: *mut Task = taskGet(grant.owner)
    if grant.waiting && task != null && task.state == TASK_BLOCKED && task.waitReason == WAIT_IRQ {
        task.context.regs[REG_RESULT] = 0
        if !taskWake(grant.owner) {
            panic("could not wake IRQ waiter", null)
            return false
        }
        // The blocked wait itself consumes the notification exactly once.
        grant.pending = false
        grant.waiting = false
    }
    return true
}

let irqWait(frame: *mut TrapFrame, token: UWord, seconds: UWord): *TrapFrame {
    objectAssertAtomic()
    let line: UWord = irqLookup(currentTask.id, token)
    let mut result: Word = -ERRNO_EPERM
    if line < PIC_LINE_COUNT {
        let grant: *mut IrqGrant = &mut irqGrants[line]
        if grant.pending {
            grant.pending = false
            result = 0
        } else if grant.inService result = -ERRNO_EBUSY
        else if seconds > 60 result = -ERRNO_EINVAL
        else {
            grant.timed = seconds != 0
            if grant.timed && !timerDeadline(seconds, &mut grant.deadline) {
                result = -ERRNO_EINVAL
                grant.timed = false
            } else {
                // Publish the exact line under the same exclusion as block.
                // WAIT_IRQ alone does not identify a multi-line owner's wait.
                grant.waiting = true
                return taskBlock(frame, WAIT_IRQ)
            }
        }
    }
    frame.regs[REG_RESULT] = result as UWord
    taskSaveContext(frame)
    return frame
}

let irqComplete(owner: UWord, token: UWord): Word {
    objectAssertAtomic()
    let line: UWord = irqLookup(owner, token)
    if line == PIC_LINE_COUNT return -ERRNO_EPERM
    let grant: *mut IrqGrant = &mut irqGrants[line]
    if !grant.inService || grant.pending return -ERRNO_EBUSY
    let pending: *volatile UWord = PIC_PENDING as *volatile UWord
    if pending[0] & (1 as UWord << line) != 0 return -ERRNO_EBUSY
    grant.inService = false
    grant.waiting = false
    grant.timed = false
    let enabled: *volatile mut UWord = PIC_ENABLE as *volatile mut UWord
    enabled[0] |= 1 as UWord << line
    fence()
    return 0
}

// Kernel-only acknowledgement after polling a device and draining its level.
// The level check in irqComplete keeps late/unserviced events masked.
let irqPollComplete(owner: UWord, token: UWord): Word {
    objectAssertAtomic()
    let line: UWord = irqLookup(owner, token)
    if line >= PIC_LINE_COUNT return -ERRNO_EPERM
    irqMask(line)
    let grant: *mut IrqGrant = &mut irqGrants[line]
    grant.pending = false
    grant.inService = true
    return irqComplete(owner, token)
}

// Timer progress bounds a blocked service wait even if a device never finishes.
let irqTimerTick(): Void {
    let mut hasWait: Bool = false
    for line: UWord in 0..PIC_LINE_COUNT {
        if irqGrants[line].owner != 0 && irqGrants[line].timed hasWait = true
    }
    if !hasWait return
    let mut now: TimerCount
    timerReadCount(&mut now)
    for line: UWord in 0..PIC_LINE_COUNT {
        let grant: *mut IrqGrant = &mut irqGrants[line]
        if grant.owner == 0 || !grant.timed || !timerDeadlineReached(&now, &grant.deadline) continue
        grant.timed = false
        irqMask(line)
        grant.inService = true
        let task: *mut Task = taskGet(grant.owner)
        if grant.waiting && task != null && task.state == TASK_BLOCKED && task.waitReason == WAIT_IRQ {
            grant.waiting = false
            task.context.regs[REG_RESULT] = (-ERRNO_ETIMEDOUT) as UWord
            if !taskWake(grant.owner) panic("could not wake expired IRQ waiter", null)
        }
    }
}

let irqReleaseTask(owner: UWord): Void {
    objectAssertAtomic()
    for line: UWord in 0..PIC_LINE_COUNT {
        if irqGrants[line].owner != owner continue
        irqMask(line)
        irqGrants[line].owner = 0
        irqGrants[line].pending = false
        irqGrants[line].inService = false
        irqGrants[line].waiting = false
        irqGrants[line].timed = false
    }
}

export { irqPollComplete, irqGrant, irqSeal, irqTokenValid, irqNotify, irqWait, irqComplete,
    irqTimerTick, irqReleaseTask }

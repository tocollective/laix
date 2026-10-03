import { STACK_CANARY, KERNEL_STACK_BOTTOM, KERNEL_STACK_TOP, CAUSE_BREAKPOINT, CAUSE_SYSCALL, CAUSE_INTERRUPT,
    INSTRUCTION_BYTES, REG_RESULT, REG_SYSCALL, ERRNO_ENOSYS, ERRNO_EINVAL,
    SYS_DEBUG_PUT_CHAR, SYS_EXIT, SYS_YIELD, SYS_HANDLE_CLOSE, SYS_HANDLE_COPY,
    SYS_ENDPOINT_DESTROY, SYS_IPC_SEND, SYS_IPC_RECEIVE, SYS_IPC_CALL, SYS_IPC_ACCEPT,
    SYS_IPC_REPLY, STATUS_PUM } from "../arch/wrm081632/defs.m"
import { TrapFrame } from "trap_frame.m"
import { panic } from "../kernel/panic.m"
import { taskSaveContext, taskOwnsTrap, taskFinish, taskYield, taskTick } from "../task/task.m"
import { timerInterrupt } from "../drivers/timer.m"
import { debugPutChar } from "../drivers/debug_uart.m"
import { ipcClose, ipcCopy, ipcDestroy, ipcSend, ipcReceive, ipcCall, ipcAccept, ipcReply } from "../ipc/ipc.m"

extern let trapEntry(): Void
extern let trapRegisterSelfTest(): Word
// BSS starts at zero = nothing expected: BREAK and SYSCALL are never cause 0.
let NO_EXPECTED_TRAP: UWord = 0
let mut expectedTrap: UWord

// Arms one supervisor BREAK or SYSCALL for a self-test. Any other one is a
// kernel bug and panics instead of being skipped.
let trapExpect(cause: UWord): Void {
    expectedTrap = cause
}

let trapExpectationMet(): Bool {
    return expectedTrap == NO_EXPECTED_TRAP
}

let takeExpectedTrap(frame: *TrapFrame): Bool {
    if frame.status & STATUS_PUM != 0 || frame.cause != expectedTrap return false
    expectedTrap = NO_EXPECTED_TRAP
    return true
}

let userSyscall(frame: *mut TrapFrame): *TrapFrame {
    // Read the saved ABI registers, never live dispatcher argument registers.
    // All outcomes consume this instruction exactly once, including exit.
    frame.epc += INSTRUCTION_BYTES
    switch frame.regs[REG_SYSCALL] {
        case SYS_DEBUG_PUT_CHAR: {
            let code: UWord = frame.regs[1]
            if code > 255 frame.regs[REG_RESULT] = (-ERRNO_EINVAL) as UWord
            else {
                debugPutChar(code)
                frame.regs[REG_RESULT] = 0
            }
            taskSaveContext(frame)
            return frame
        }
        case SYS_EXIT:
            return taskFinish(frame, frame.regs[1] as Word, false)
        case SYS_YIELD:
            frame.regs[REG_RESULT] = 0
            return taskYield(frame)
        case SYS_HANDLE_CLOSE:
            frame.regs[REG_RESULT] = ipcClose(frame.regs[1]) as UWord
            taskSaveContext(frame)
            return frame
        case SYS_HANDLE_COPY:
            frame.regs[REG_RESULT] = ipcCopy(frame.regs[1], frame.regs[2], frame.regs[3]) as UWord
            taskSaveContext(frame)
            return frame
        case SYS_IPC_SEND:
            return ipcSend(frame, frame.regs[1], frame.regs[2], frame.regs[3])
        case SYS_IPC_RECEIVE:
            return ipcReceive(frame, frame.regs[1], frame.regs[2], frame.regs[3])
        case SYS_IPC_CALL:
            return ipcCall(frame, frame.regs[1], frame.regs[2], frame.regs[3], frame.regs[4], frame.regs[5])
        case SYS_IPC_ACCEPT:
            return ipcAccept(frame, frame.regs[1], frame.regs[2], frame.regs[3])
        case SYS_IPC_REPLY:
            return ipcReply(frame, frame.regs[1], frame.regs[2], frame.regs[3])
        case SYS_ENDPOINT_DESTROY:
            frame.regs[REG_RESULT] = ipcDestroy(frame.regs[1]) as UWord
            taskSaveContext(frame)
            return frame
        default:
            frame.regs[REG_RESULT] = (-ERRNO_ENOSYS) as UWord
            taskSaveContext(frame)
            return frame
    }
    return frame
}

let trapDispatch(frame: *mut TrapFrame): *TrapFrame {
    // The entry selected and checked the current task's trusted kernel stack.
    // No nesting: low entry state remains stable until IRET.
    let bottomSlot: *UWord = KERNEL_STACK_BOTTOM as *UWord
    let bottom: *UWord = *bottomSlot as *UWord
    if *bottom != STACK_CANARY {
        panic("kernel stack canary damaged", frame)
        return frame
    }
    if frame.status & STATUS_PUM != 0 {
        if !taskOwnsTrap(frame) {
            panic("user trap without running task", frame)
            return frame
        }
        taskSaveContext(frame)
    }
    // IRQs have a separate device acknowledgement and scheduling path.
    if frame.cause == CAUSE_INTERRUPT {
        if timerInterrupt() return taskTick(frame)
        return frame
    }
    if frame.status & STATUS_PUM != 0 {
        if frame.cause == CAUSE_SYSCALL return userSyscall(frame)
        return taskFinish(frame, frame.cause as Word, true)
    }
    switch frame.cause {
        case CAUSE_BREAKPOINT: {
            if !takeExpectedTrap(frame) {
                panic("unexpected breakpoint", frame)
                return frame
            }
            frame.epc += INSTRUCTION_BYTES
            return frame
        }
        case CAUSE_SYSCALL: {
            if !takeExpectedTrap(frame) {
                panic("unexpected syscall", frame)
                return frame
            }
            frame.regs[REG_RESULT] = (-ERRNO_ENOSYS) as UWord
            frame.epc += INSTRUCTION_BYTES
            return frame
        }
        default: panic("unexpected exception", frame)
    }
    return frame
}

// trapEntry's .bad_stack path, on its static emergency stack after the early
// UART line: the trusted kernel stack failed its checks. frame is a static
// copy of the interrupted context; its r30 is the interrupted sp.
let trapBadStack(frame: *TrapFrame, rejectedSp: UWord): Void {
    let bottom: *UWord = KERNEL_STACK_BOTTOM as *UWord
    let top: *UWord = KERNEL_STACK_TOP as *UWord
    panic("invalid kernel stack: sp=$h, bounds $h..$h", frame, rejectedSp, *bottom, *top)
}

export { trapEntry, trapRegisterSelfTest, trapDispatch, trapBadStack, trapExpect,
    trapExpectationMet }

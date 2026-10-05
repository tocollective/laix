import { transferReserve, transferCommit, transferCancel, transferCollect } from "../ipc/transfer.m"
import { SYS_TRANSFER_RESERVE, SYS_TRANSFER_COMMIT, SYS_TRANSFER_CANCEL, SYS_TRANSFER_COLLECT } from "../arch/wrm081632/defs.m"
import { servicePublish, serviceResolve, serviceAllow, serviceWithdraw, serviceConfigure } from "../task/recovery.m"
import { SYS_SERVICE_PUBLISH, SYS_SERVICE_RESOLVE, SYS_SERVICE_ALLOW,
    SYS_SERVICE_WITHDRAW, SYS_SERVICE_CONFIGURE } from "../arch/wrm081632/defs.m"
import { SYS_IPC_CALL_TIMED, SYS_IPC_TRY_SEND, SYS_IPC_TRY_RECEIVE,
    SYS_IPC_TRY_ACCEPT, SYS_TASK_CANCEL_WAIT, SYS_SLEEP } from "../arch/wrm081632/defs.m"
import { ipcCallTimed, ipcSendMode, ipcReceiveMode, ipcAcceptMode,
    ipcSupervisorCancel, ipcSleep } from "../ipc/ipc.m"
import { SYS_ENDPOINT_CREATE, SYS_TASK_DEVICES } from "../arch/wrm081632/defs.m"
import { ipcCreate } from "../ipc/ipc.m"
import { taskRuntimeDevices } from "../task/control.m"
import { SYS_MEM_GRANT, SYS_MEM_GRANT_MAP, SYS_MEM_GRANT_CLOSE } from "../arch/wrm081632/defs.m"
import { memoryCreateGrant, memoryMapGrant, memoryCloseGrant } from "../mm/sharing.m"
import { SYS_MEM_SPACE, SYS_MEM_ALLOC, SYS_MEM_RELEASE, SYS_MEM_MAP,
    SYS_MEM_UNMAP, SYS_MEM_PROTECT, SYS_MEM_POPULATE, SYS_MEM_CLOSE } from "../arch/wrm081632/defs.m"
import { memoryOpenSpace, memoryCloseSpace, memoryAllocate, memoryRelease,
    memoryMap, memoryEdit, memoryPopulate } from "../mm/runtime.m"
import { SYS_TASK_CREATE, SYS_TASK_CONFIGURE, SYS_TASK_PUBLISH, SYS_TASK_INSPECT,
    SYS_TASK_TERMINATE, SYS_TASK_COLLECT } from "../arch/wrm081632/defs.m"
import { taskRuntimeCreate, taskRuntimeConfigure, taskRuntimePublish, taskRuntimeRead,
    taskRuntimeTerminate } from "../task/control.m"
import { STACK_CANARY, KERNEL_STACK_BOTTOM, KERNEL_STACK_TOP, CAUSE_BREAKPOINT, CAUSE_SYSCALL, CAUSE_INTERRUPT,
    INSTRUCTION_BYTES, REG_RESULT, REG_SYSCALL, ERRNO_ENOSYS, ERRNO_EINVAL,
    SYS_DEBUG_PUT_CHAR, SYS_EXIT, SYS_YIELD, SYS_HANDLE_CLOSE, SYS_HANDLE_COPY,
    SYS_ENDPOINT_DESTROY, SYS_IPC_SEND, SYS_IPC_RECEIVE, SYS_IPC_CALL, SYS_IPC_ACCEPT,
    SYS_IPC_REPLY, STATUS_PUM, ERRNO_EPERM, DEVICE_UART_TX } from "../arch/wrm081632/defs.m"
import { TrapFrame } from "trap_frame.m"
import { panic } from "../kernel/panic.m"
import { currentTask, taskSaveContext, taskOwnsTrap, taskFinish, taskYield, taskTick } from "../task/task.m"
import { timerInterrupt } from "../drivers/timer.m"
import { irqWait, irqComplete } from "../drivers/irq.m"
import { screenControl, fontValidate, fontBegin, fontFinish, fontCancelOwner } from "../drivers/service_devices.m"
import { inputRead } from "../drivers/input_device.m"
import { diskInfo, diskBegin } from "../drivers/service_devices.m"
import { SYS_INPUT_READ, SYS_DISK_INFO, SYS_DISK_BEGIN, SYS_DISK_FINISH,
    SYS_DISK_CANCEL, DEVICE_INPUT, DEVICE_DISK } from "../arch/wrm081632/defs.m"
import { taskIrqReturn } from "../task/task.m"
import { SYS_IRQ_WAIT, SYS_IRQ_COMPLETE, SYS_SCREEN_CONTROL, SYS_FONT_VALIDATE,
    SYS_FONT_BEGIN, SYS_FONT_FINISH, SYS_FONT_CANCEL, DEVICE_SCREEN,
    DEVICE_FONT } from "../arch/wrm081632/defs.m"
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
            if currentTask == null || currentTask.deviceRights & DEVICE_UART_TX == 0 {
                frame.regs[REG_RESULT] = (-ERRNO_EPERM) as UWord
            } else if code > 255 frame.regs[REG_RESULT] = (-ERRNO_EINVAL) as UWord
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
        case SYS_TRANSFER_RESERVE:
            frame.regs[REG_RESULT] = transferReserve(frame.regs[1], frame.regs[2], frame.regs[3], frame.regs[4]) as UWord
            taskSaveContext(frame)
            return frame
        case SYS_TRANSFER_COMMIT:
            frame.regs[REG_RESULT] = transferCommit(frame.regs[1], frame.regs[2], frame.regs[3], frame.regs[4]) as UWord
            taskSaveContext(frame)
            return frame
        case SYS_TRANSFER_CANCEL:
            frame.regs[REG_RESULT] = transferCancel(frame.regs[1]) as UWord
            taskSaveContext(frame)
            return frame
        case SYS_TRANSFER_COLLECT:
            return transferCollect(frame, frame.regs[1])
        case SYS_HANDLE_COPY:
            frame.regs[REG_RESULT] = ipcCopy(frame.regs[1], frame.regs[2], frame.regs[3]) as UWord
            taskSaveContext(frame)
            return frame
        case SYS_IPC_SEND:
            return ipcSend(frame, frame.regs[1], frame.regs[2], frame.regs[3])
        case SYS_IPC_RECEIVE:
            return ipcReceive(frame, frame.regs[1], frame.regs[2], frame.regs[3])
        case SYS_IPC_CALL_TIMED:
            return ipcCallTimed(frame, frame.regs[1], frame.regs[2], frame.regs[3], frame.regs[4], frame.regs[5], frame.regs[6])
        case SYS_IPC_TRY_SEND:
            return ipcSendMode(frame, frame.regs[1], frame.regs[2], frame.regs[3], true)
        case SYS_IPC_TRY_RECEIVE:
            return ipcReceiveMode(frame, frame.regs[1], frame.regs[2], frame.regs[3], true)
        case SYS_IPC_TRY_ACCEPT:
            return ipcAcceptMode(frame, frame.regs[1], frame.regs[2], frame.regs[3], true)
        case SYS_TASK_CANCEL_WAIT:
            return deviceResult(frame, ipcSupervisorCancel(frame.regs[1]))
        case SYS_SLEEP:
            return ipcSleep(frame, frame.regs[1])
        case SYS_IPC_CALL:
            return ipcCall(frame, frame.regs[1], frame.regs[2], frame.regs[3], frame.regs[4], frame.regs[5])
        case SYS_IPC_ACCEPT:
            return ipcAccept(frame, frame.regs[1], frame.regs[2], frame.regs[3])
        case SYS_IPC_REPLY:
            return ipcReply(frame, frame.regs[1], frame.regs[2], frame.regs[3])
        case SYS_ENDPOINT_CREATE:
            return deviceResult(frame, ipcCreate(frame.regs[1], frame.regs[2]))
        case SYS_TASK_DEVICES:
            return deviceResult(frame, taskRuntimeDevices(frame.regs[1], frame.regs[2]))
        case SYS_ENDPOINT_DESTROY:
            frame.regs[REG_RESULT] = ipcDestroy(frame.regs[1]) as UWord
            taskSaveContext(frame)
            return frame
        case SYS_MEM_GRANT:
            return deviceResult(frame, memoryCreateGrant(frame.regs[1], frame.regs[2], frame.regs[3], frame.regs[4]))
        case SYS_MEM_GRANT_MAP:
            return deviceResult(frame, memoryMapGrant(frame.regs[1], frame.regs[2], frame.regs[3], frame.regs[4]))
        case SYS_MEM_GRANT_CLOSE:
            return deviceResult(frame, memoryCloseGrant(frame.regs[1]))
        case SYS_MEM_SPACE:
            return deviceResult(frame, memoryOpenSpace(frame.regs[1], frame.regs[2]))
        case SYS_MEM_CLOSE:
            return deviceResult(frame, memoryCloseSpace(frame.regs[1]))
        case SYS_MEM_ALLOC:
            return deviceResult(frame, memoryAllocate(frame.regs[1], frame.regs[2]))
        case SYS_MEM_RELEASE:
            return deviceResult(frame, memoryRelease(frame.regs[1], frame.regs[2]))
        case SYS_MEM_MAP:
            return deviceResult(frame, memoryMap(frame.regs[1], frame.regs[2], frame.regs[3], frame.regs[4], frame.regs[5], frame.regs[6]))
        case SYS_MEM_UNMAP:
            return deviceResult(frame, memoryEdit(frame.regs[1], frame.regs[2], frame.regs[3], 0, false))
        case SYS_MEM_PROTECT:
            return deviceResult(frame, memoryEdit(frame.regs[1], frame.regs[2], frame.regs[3], frame.regs[4], true))
        case SYS_MEM_POPULATE:
            return deviceResult(frame, memoryPopulate(frame.regs[1], frame.regs[2], frame.regs[3], frame.regs[4], frame.regs[5]))
        case SYS_SERVICE_PUBLISH:
            return deviceResult(frame, servicePublish(frame.regs[1], frame.regs[2], frame.regs[3], frame.regs[4]))
        case SYS_SERVICE_RESOLVE:
            return deviceResult(frame, serviceResolve(frame.regs[1], frame.regs[2]))
        case SYS_SERVICE_ALLOW:
            return deviceResult(frame, serviceAllow(frame.regs[1], frame.regs[2]))
        case SYS_SERVICE_WITHDRAW:
            return deviceResult(frame, serviceWithdraw(frame.regs[1], frame.regs[2] as Word))
        case SYS_SERVICE_CONFIGURE:
            return deviceResult(frame, serviceConfigure(frame.regs[1], frame.regs[2], frame.regs[3], frame.regs[4], frame.regs[5]))
        case SYS_TASK_CREATE:
            return deviceResult(frame, taskRuntimeCreate(frame.regs[1]))
        case SYS_TASK_CONFIGURE:
            return deviceResult(frame, taskRuntimeConfigure(frame.regs[1], frame.regs[2], frame.regs[3], frame.regs[4]))
        case SYS_TASK_PUBLISH:
            return deviceResult(frame, taskRuntimePublish(frame.regs[1]))
        case SYS_TASK_INSPECT:
            return deviceResult(frame, taskRuntimeRead(frame.regs[1], frame.regs[2], false))
        case SYS_TASK_COLLECT:
            return deviceResult(frame, taskRuntimeRead(frame.regs[1], frame.regs[2], true))
        case SYS_TASK_TERMINATE:
            return taskRuntimeTerminate(frame, frame.regs[1], frame.regs[2] as Word)
        case SYS_IRQ_WAIT:
            return irqWait(frame, frame.regs[1], frame.regs[2])
        case SYS_IRQ_COMPLETE:
            return deviceResult(frame, irqComplete(currentTask.id, frame.regs[1]))
        case SYS_SCREEN_CONTROL:
            if currentTask.deviceRights != DEVICE_SCREEN return deviceResult(frame, -ERRNO_EPERM)
            return deviceResult(frame, screenControl(currentTask.id, frame.regs[1]))
        case SYS_FONT_VALIDATE:
            if currentTask.deviceRights != DEVICE_FONT return deviceResult(frame, -ERRNO_EPERM)
            return deviceResult(frame, fontValidate(currentTask.id))
        case SYS_FONT_BEGIN:
            if currentTask.deviceRights != DEVICE_FONT return deviceResult(frame, -ERRNO_EPERM)
            return deviceResult(frame, fontBegin(currentTask.id, frame.regs[1], frame.regs[2]))
        case SYS_FONT_FINISH:
            if currentTask.deviceRights != DEVICE_FONT return deviceResult(frame, -ERRNO_EPERM)
            return deviceResult(frame, fontFinish(currentTask.id, frame.regs[1]))
        case SYS_FONT_CANCEL:
            if currentTask.deviceRights != DEVICE_FONT return deviceResult(frame, -ERRNO_EPERM)
            fontCancelOwner(currentTask.id)
            return deviceResult(frame, 0)
        case SYS_INPUT_READ:
            if currentTask.deviceRights != DEVICE_INPUT return deviceResult(frame, -ERRNO_EPERM)
            return deviceResult(frame, inputRead(currentTask.id, frame.regs[1]))
        case SYS_DISK_INFO:
            if currentTask.deviceRights != DEVICE_DISK return deviceResult(frame, -ERRNO_EPERM)
            return deviceResult(frame, diskInfo(currentTask.id))
        case SYS_DISK_BEGIN:
            if currentTask.deviceRights != DEVICE_DISK return deviceResult(frame, -ERRNO_EPERM)
            return deviceResult(frame, diskBegin(currentTask.id, frame.regs[1], frame.regs[2]))
        case SYS_DISK_FINISH:
            if currentTask.deviceRights != DEVICE_DISK return deviceResult(frame, -ERRNO_EPERM)
            return deviceResult(frame, fontFinish(currentTask.id, frame.regs[1]))
        case SYS_DISK_CANCEL:
            if currentTask.deviceRights != DEVICE_DISK return deviceResult(frame, -ERRNO_EPERM)
            fontCancelOwner(currentTask.id)
            return deviceResult(frame, 0)
        default:
            frame.regs[REG_RESULT] = (-ERRNO_ENOSYS) as UWord
            taskSaveContext(frame)
            return frame
    }
    return frame
}

let deviceResult(frame: *mut TrapFrame, result: Word): *TrapFrame {
    frame.regs[REG_RESULT] = result as UWord
    taskSaveContext(frame)
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
        return taskIrqReturn(frame)
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

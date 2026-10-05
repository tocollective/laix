// User-only wrappers: import constants, never kernel code or the boot runtime.
import { SYS_DEBUG_PUT_CHAR, SYS_EXIT, SYS_YIELD, SYS_HANDLE_CLOSE, SYS_HANDLE_COPY,
    SYS_ENDPOINT_DESTROY, SYS_IPC_SEND, SYS_IPC_RECEIVE, SYS_IPC_CALL, SYS_IPC_REPLY } from "../src/arch/wrm081632/defs.m"
import { SYS_IRQ_WAIT, SYS_IRQ_COMPLETE, SYS_SCREEN_CONTROL, SYS_FONT_BEGIN,
    SYS_FONT_FINISH, SYS_FONT_CANCEL, SYS_FONT_VALIDATE } from "../src/arch/wrm081632/defs.m"

type AcceptResult {
    length: Word,
    replyToken: UWord,
}

// Assembly captures r2 before an M call can discard the second result word.
extern let ipcAcceptResult(handle: UWord, buffer: *mut UByte, capacity: UWord, result: *mut AcceptResult): Word

// One UART byte, 0..255. Requires the init-granted DEVICE_UART_TX operation;
// returns 0, -EPERM or -EINVAL. No user pointer or MMIO address is accepted.
let debugPutChar(code: UWord): Word {
    return syscall(SYS_DEBUG_PUT_CHAR, code)
}

// No successful return. Stay in user mode if a broken kernel returns anyway.
let exit(code: Word): Void {
    syscall(SYS_EXIT, code)
    while true {}
}

// Voluntarily rotate the ready queue. Returns 0 when this task resumes.
let yield(): Word {
    return syscall(SYS_YIELD)
}

let closeHandle(handle: UWord): Word {
    return syscall(SYS_HANDLE_CLOSE, handle)
}

// Returns a positive handle in targetTask's table, or a negative errno.
// The caller communicates the returned number to the recipient separately.
let copyHandle(handle: UWord, targetTask: UWord, rights: UWord): Word {
    return syscall(SYS_HANDLE_COPY, handle, targetTask, rights)
}

let destroyEndpoint(handle: UWord): Word {
    return syscall(SYS_ENDPOINT_DESTROY, handle)
}

// Returns the delivered byte count or -errno. Both calls may block.
let send(handle: UWord, buffer: *UByte, length: UWord): Word {
    return syscall(SYS_IPC_SEND, handle, buffer, length)
}

// -EMSGSIZE leaves the message pending; retry with IPC_MESSAGE_MAX capacity.
// The raw ABI additionally reports the required size in r2 on that error.
let recv(handle: UWord, buffer: *mut UByte, capacity: UWord): Word {
    return syscall(SYS_IPC_RECEIVE, handle, buffer, capacity)
}

let call(handle: UWord, request: *UByte, length: UWord, response: *mut UByte, capacity: UWord): Word {
    return syscall(SYS_IPC_CALL, handle, request, length, response, capacity)
}

// result must point to a writable, word-aligned AcceptResult in this task.
// On error it contains the negative length and a zero reply token.
let accept(handle: UWord, buffer: *mut UByte, capacity: UWord, result: *mut AcceptResult): Word {
    return ipcAcceptResult(handle, buffer, capacity, result)
}

let reply(token: UWord, response: *UByte, length: UWord): Word {
    return syscall(SYS_IPC_REPLY, token, response, length)
}

export { AcceptResult, debugPutChar, exit, yield, closeHandle, copyHandle, destroyEndpoint, send, recv, call, accept, reply }

let irqWait(token: UWord, seconds: UWord): Word { return syscall(SYS_IRQ_WAIT, token, seconds) }
let irqComplete(token: UWord): Word { return syscall(SYS_IRQ_COMPLETE, token) }
let screenControl(operation: UWord): Word { return syscall(SYS_SCREEN_CONTROL, operation) }
let fontBegin(glyph: UWord, chunk: UWord): Word { return syscall(SYS_FONT_BEGIN, glyph, chunk) }
let fontFinish(destination: *mut UByte): Word { return syscall(SYS_FONT_FINISH, destination) }
let fontCancel(): Word { return syscall(SYS_FONT_CANCEL) }
let fontValidate(): Word { return syscall(SYS_FONT_VALIDATE) }

export { irqWait, irqComplete, screenControl, fontBegin, fontFinish, fontCancel, fontValidate }

import { SYS_INPUT_READ, SYS_DISK_INFO, SYS_DISK_BEGIN, SYS_DISK_FINISH,
    SYS_DISK_CANCEL } from "../src/arch/wrm081632/defs.m"
let inputRead(destination: *mut UByte): Word { return syscall(SYS_INPUT_READ, destination) }
let diskInfo(): Word { return syscall(SYS_DISK_INFO) }
let diskBegin(offset: UWord, bytes: UWord): Word { return syscall(SYS_DISK_BEGIN, offset, bytes) }
let diskFinish(destination: *mut UByte): Word { return syscall(SYS_DISK_FINISH, destination) }
let diskCancel(): Word { return syscall(SYS_DISK_CANCEL) }
export { inputRead, diskInfo, diskBegin, diskFinish, diskCancel }

import { SYS_TASK_CREATE, SYS_TASK_CONFIGURE, SYS_TASK_PUBLISH, SYS_TASK_INSPECT,
    SYS_TASK_TERMINATE, SYS_TASK_COLLECT } from "../src/arch/wrm081632/defs.m"
import { TaskEvent } from "../src/task/runtime_start.m"
let createTask(image: UWord): Word { return syscall(SYS_TASK_CREATE, image) }
let configureTask(reference: UWord, endpoint: UWord, rights: UWord, argument: UWord): Word {
    return syscall(SYS_TASK_CONFIGURE, reference, endpoint, rights, argument)
}
let publishTask(reference: UWord): Word { return syscall(SYS_TASK_PUBLISH, reference) }
let inspectTask(reference: UWord, event: *mut TaskEvent): Word { return syscall(SYS_TASK_INSPECT, reference, event) }
let terminateTask(reference: UWord, code: Word): Word { return syscall(SYS_TASK_TERMINATE, reference, code) }
let collectTask(reference: UWord, event: *mut TaskEvent): Word { return syscall(SYS_TASK_COLLECT, reference, event) }
export { createTask, configureTask, publishTask, inspectTask, terminateTask, collectTask }

import { SYS_MEM_SPACE, SYS_MEM_ALLOC, SYS_MEM_RELEASE, SYS_MEM_MAP,
    SYS_MEM_UNMAP, SYS_MEM_PROTECT, SYS_MEM_POPULATE, SYS_MEM_CLOSE } from "../src/arch/wrm081632/defs.m"
// Target zero names self; foreign targets require unpublished-task authority.
let openMemorySpace(target: UWord, rights: UWord): Word { return syscall(SYS_MEM_SPACE, target, rights) }
let closeMemorySpace(space: UWord): Word { return syscall(SYS_MEM_CLOSE, space) }
let allocateRegion(space: UWord, pages: UWord): Word { return syscall(SYS_MEM_ALLOC, space, pages) }
let releaseRegion(space: UWord, region: UWord): Word { return syscall(SYS_MEM_RELEASE, space, region) }
let mapRegion(space: UWord, region: UWord, virtual: UWord, offset: UWord, pages: UWord, permissions: UWord): Word {
    return syscall(SYS_MEM_MAP, space, region, virtual, offset, pages, permissions)
}
let unmapRegion(space: UWord, virtual: UWord, pages: UWord): Word { return syscall(SYS_MEM_UNMAP, space, virtual, pages) }
let protectRegion(space: UWord, virtual: UWord, pages: UWord, permissions: UWord): Word {
    return syscall(SYS_MEM_PROTECT, space, virtual, pages, permissions)
}
let populateRegion(space: UWord, region: UWord, offset: UWord, source: *UByte, bytes: UWord): Word {
    return syscall(SYS_MEM_POPULATE, space, region, offset, source, bytes)
}
export { openMemorySpace, closeMemorySpace, allocateRegion, releaseRegion, mapRegion,
    unmapRegion, protectRegion, populateRegion }

import { SYS_MEM_GRANT, SYS_MEM_GRANT_MAP, SYS_MEM_GRANT_CLOSE } from "../src/arch/wrm081632/defs.m"
// The owner communicates the returned grant token to the named borrower.
let grantRegion(space: UWord, region: UWord, borrower: UWord, permissions: UWord): Word {
    return syscall(SYS_MEM_GRANT, space, region, borrower, permissions)
}
let mapGrantedRegion(space: UWord, grant: UWord, virtual: UWord, permissions: UWord): Word {
    return syscall(SYS_MEM_GRANT_MAP, space, grant, virtual, permissions)
}
let closeMemoryGrant(grant: UWord): Word { return syscall(SYS_MEM_GRANT_CLOSE, grant) }
export { grantRegion, mapGrantedRegion, closeMemoryGrant }

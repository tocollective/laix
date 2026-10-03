// User-only wrappers: import constants, never kernel code or the boot runtime.
import { SYS_DEBUG_PUT_CHAR, SYS_EXIT, SYS_YIELD, SYS_HANDLE_CLOSE, SYS_HANDLE_COPY,
    SYS_ENDPOINT_DESTROY, SYS_IPC_SEND, SYS_IPC_RECEIVE, SYS_IPC_CALL, SYS_IPC_REPLY } from "../src/arch/wrm081632/defs.m"

type AcceptResult {
    length: Word,
    replyToken: UWord,
}

// Assembly captures r2 before an M call can discard the second result word.
extern let ipcAcceptResult(handle: UWord, buffer: *mut UByte, capacity: UWord, result: *mut AcceptResult): Word

// One UART byte, 0..255. Returns 0 or -EINVAL; no user pointer is read.
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

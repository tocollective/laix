// User-only wrappers: import constants, never kernel code or the boot runtime.
import { SYS_DEBUG_PUT_CHAR, SYS_EXIT, SYS_YIELD, SYS_HANDLE_CLOSE, SYS_HANDLE_COPY,
    SYS_ENDPOINT_DESTROY, SYS_IPC_SEND, SYS_IPC_RECEIVE, SYS_IPC_CALL, SYS_IPC_REPLY } from "../src/arch/wrm081632/defs.m"
import { SYS_IRQ_WAIT, SYS_IRQ_COMPLETE, SYS_SCREEN_CONTROL, DISK_READ,
    GLYPH_BYTES, ERRNO_EINVAL, SYS_DEVICE_INFO, SYS_DEVICE_SUBMIT,
    SYS_DEVICE_FINISH, SYS_DEVICE_CANCEL, SYS_DEVICE_EXTENT, SYS_DEVICE_FLAGS, DISK_WRITE, DISK_FLUSH } from "../src/arch/wrm081632/defs.m"

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

// Self-table copy only. Foreign task IDs return -EPERM; use consent below.
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
let screenControl(register: UWord, value: UWord): Word { return syscall(SYS_SCREEN_CONTROL, register, value) }
// Font-specific offsets and chunk validation are entirely user policy.
let fontBegin(glyph: UWord, chunk: UWord): Word {
    if chunk > 1 || glyph > (0x7FFFFFFF - 16) / GLYPH_BYTES return -ERRNO_EINVAL
    return diskBegin(glyph * GLYPH_BYTES + chunk * 16, 16)
}
let fontFinish(instance: UWord, destination: *mut UByte): Word { return diskFinish(instance, destination) }
let fontCancel(instance: UWord): Word { return diskCancel(instance) }
let fontValidate(): Word {
    let result: Word = diskInfo()
    if result < 0 return result
    return 0
}
export { irqWait, irqComplete, screenControl, fontBegin, fontFinish, fontCancel, fontValidate }

import { SYS_INPUT_READ } from "../src/arch/wrm081632/defs.m"
let inputRead(destination: *mut UByte, capacity: UWord): Word { return syscall(SYS_INPUT_READ, destination, capacity) }
let diskInfo(): Word { return syscall(SYS_DEVICE_INFO) }
let diskBegin(offset: UWord, bytes: UWord): Word { return syscall(SYS_DEVICE_SUBMIT, offset, bytes, DISK_READ, 0) }
// WRITE moves whole sectors (offset and bytes multiples of 512, at most 4096)
// from `source`; the kernel copies them before it issues the command.
let diskWriteBegin(offset: UWord, bytes: UWord, source: *UByte): Word {
    return syscall(SYS_DEVICE_SUBMIT, offset, bytes, DISK_WRITE, source)
}
let diskFlushBegin(): Word { return syscall(SYS_DEVICE_SUBMIT, 0, 0, DISK_FLUSH, 0) }
let diskFlags(): Word { return syscall(SYS_DEVICE_FLAGS) }
let diskFinish(instance: UWord, destination: *mut UByte): Word { return syscall(SYS_DEVICE_FINISH, instance, destination) }
let diskCancel(instance: UWord): Word { return syscall(SYS_DEVICE_CANCEL, instance) }
let grantDeviceExtent(reference: UWord, offset: UWord, bytes: UWord, flags: UWord): Word {
    return syscall(SYS_DEVICE_EXTENT, reference, offset, bytes, flags)
}
export { grantDeviceExtent }

import { SYS_LIFETIME } from "../src/arch/wrm081632/defs.m"
import { LifetimeReport } from "../src/task/runtime_start.m"
// Reference 0 reports only global retirement; a child reference (INSPECT) also
// selects that child's remaining reply, task and handle generations.
let lifetimeReport(reference: UWord, report: *mut LifetimeReport): Word {
    return syscall(SYS_LIFETIME, reference, report)
}
export { lifetimeReport }

import { SYS_NET_INFO, SYS_NET_SEND, SYS_NET_RECV } from "../src/arch/wrm081632/defs.m"
// The Ethernet broker (device right NET): 16 bytes of address and counters, one
// whole frame out, one whole frame in (capacity at least 1,514).
let netDeviceInfo(destination: *mut UByte): Word { return syscall(SYS_NET_INFO, destination) }
let netDeviceSend(frame: *UByte, length: UWord): Word { return syscall(SYS_NET_SEND, frame, length) }
let netDeviceReceive(destination: *mut UByte, capacity: UWord): Word { return syscall(SYS_NET_RECV, destination, capacity) }
export { netDeviceInfo, netDeviceSend, netDeviceReceive }

export { inputRead, diskInfo, diskBegin, diskWriteBegin, diskFlushBegin, diskFlags, diskFinish, diskCancel }

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
// Loads a child from an ELF image in the caller's memory (needs the load
// authority). Result: a reference like createTask, or -EINVAL/-EFAULT/-ENFILE.
import { SYS_TASK_LOAD } from "../src/arch/wrm081632/defs.m"
let loadTask(image: *UByte, bytes: UWord): Word { return syscall(SYS_TASK_LOAD, image, bytes) }
export { createTask, loadTask, configureTask, publishTask, inspectTask, terminateTask, collectTask }

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

import { SYS_ENDPOINT_CREATE, SYS_TASK_DEVICES } from "../src/arch/wrm081632/defs.m"
// Receiver zero selects self; foreign Service receivers must be owned Created children.
let createEndpoint(mode: UWord, receiver: UWord): Word { return syscall(SYS_ENDPOINT_CREATE, mode, receiver) }
// Returns the keyboard IRQ token (or zero for UART only), never an MMIO address.
let grantTaskDevices(reference: UWord, devices: UWord): Word { return syscall(SYS_TASK_DEVICES, reference, devices) }
export { createEndpoint, grantTaskDevices }

import { SYS_IPC_CALL_TIMED, SYS_IPC_TRY_SEND, SYS_IPC_TRY_RECEIVE,
    SYS_TASK_CANCEL_WAIT, SYS_SLEEP } from "../src/arch/wrm081632/defs.m"
extern let ipcTryAcceptResult(handle: UWord, buffer: *mut UByte, capacity: UWord, result: *mut AcceptResult): Word
// A single 1..60 second budget covers acceptance and reply; expiry is not rollback.
let callTimed(handle: UWord, request: *UByte, length: UWord, response: *mut UByte,
    capacity: UWord, seconds: UWord): Word {
    return syscall(SYS_IPC_CALL_TIMED, handle, request, length, response, capacity, seconds)
}
let trySend(handle: UWord, buffer: *UByte, length: UWord): Word {
    return syscall(SYS_IPC_TRY_SEND, handle, buffer, length)
}
let tryRecv(handle: UWord, buffer: *mut UByte, capacity: UWord): Word {
    return syscall(SYS_IPC_TRY_RECEIVE, handle, buffer, capacity)
}
let tryAccept(handle: UWord, buffer: *mut UByte, capacity: UWord, result: *mut AcceptResult): Word {
    return ipcTryAcceptResult(handle, buffer, capacity, result)
}
// Requires TASK_RIGHT_CANCEL for this exact task reference; cancels its current IPC/sleep wait.
let cancelTaskWait(reference: UWord): Word { return syscall(SYS_TASK_CANCEL_WAIT, reference) }
let sleep(seconds: UWord): Word { return syscall(SYS_SLEEP, seconds) }
export { callTimed, trySend, tryRecv, tryAccept, cancelTaskWait, sleep }

import { SYS_SERVICE_PUBLISH, SYS_SERVICE_RESOLVE, SYS_SERVICE_ALLOW,
    SYS_SERVICE_WITHDRAW, SYS_SERVICE_CONFIGURE } from "../src/arch/wrm081632/defs.m"
import { ServiceResolution } from "../src/task/recovery_start.m"
let publishService(name: UWord, reference: UWord, root: UWord, generation: UWord): Word {
    return syscall(SYS_SERVICE_PUBLISH, name, reference, root, generation)
}
let resolveService(name: UWord, result: *mut ServiceResolution): Word {
    return syscall(SYS_SERVICE_RESOLVE, name, result)
}
let allowServices(reference: UWord, mask: UWord): Word { return syscall(SYS_SERVICE_ALLOW, reference, mask) }
let withdrawService(name: UWord, status: Word): Word { return syscall(SYS_SERVICE_WITHDRAW, name, status) }
let configureService(reference: UWord, root: UWord, dependency: UWord, generation: UWord, irq: UWord): Word {
    return syscall(SYS_SERVICE_CONFIGURE, reference, root, dependency, generation, irq)
}
export { publishService, resolveService, allowServices, withdrawService, configureService }

// Receiver-selected slot and authenticated kernel notification, separate from
// payload IPC. TransferResult is filled even on errors (identity fields zero).
import { SYS_TRANSFER_RESERVE, SYS_TRANSFER_COMMIT, SYS_TRANSFER_CANCEL } from "../src/arch/wrm081632/defs.m"
type TransferResult { handle: Word, sender: UWord, rights: UWord }
extern let transferCollectResult(ticket: UWord, result: *mut TransferResult): Word
let reserveTransfer(slot: UWord, sender: UWord, rights: UWord, seconds: UWord): Word {
    return syscall(SYS_TRANSFER_RESERVE, slot, sender, rights, seconds)
}
let commitTransfer(source: UWord, receiver: UWord, ticket: UWord, rights: UWord): Word {
    return syscall(SYS_TRANSFER_COMMIT, source, receiver, ticket, rights)
}
let cancelTransfer(ticket: UWord): Word { return syscall(SYS_TRANSFER_CANCEL, ticket) }
let collectTransfer(ticket: UWord, result: *mut TransferResult): Word {
    return transferCollectResult(ticket, result)
}
export { TransferResult, reserveTransfer, commitTransfer, cancelTransfer, collectTransfer }

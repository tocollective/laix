import { transferTimerTick } from "transfer.m"
import { TimerCount, timerDeadline, timerReadCount, timerDeadlineReached } from "../drivers/timer.m"
import { taskControlLookup } from "../task/control.m"
import { TASK_RIGHT_CONFIGURE, TASK_RIGHT_CANCEL } from "../arch/wrm081632/defs.m"
// Public kernel entry points always resolve authority in the calling task.
import { Task, currentTask, taskGet, taskSlot, TASK_RUNNING, TASK_READY, TASK_BLOCKED,
    TASK_CREATED, taskHighWater, WAIT_NONE, WAIT_IPC_SEND, WAIT_IPC_RECEIVE, WAIT_IPC_CALL, WAIT_IPC_ACCEPT,
    WAIT_IPC_REPLY, WAIT_SLEEP, taskOwnsTrap, taskSaveContext, taskBlock, taskWake } from "../task/task.m"
import { Endpoint, Handle, handleLookup, handleClose, handleCopy, endpointDestroy, objectAssertAtomic,
    endpointFactoryCreate, handleEntry, endpointRelease, endpointWaitCapacity, ENDPOINT_RAW, ENDPOINT_SERVICE,
    ENDPOINT_LIVE } from "objects.m"
import { TrapFrame } from "../trap/trap_frame.m"
import { mmuUserBufferValid, copyFromUser, copyToUser } from "../mm/mmu.m"
import { panic } from "../kernel/panic.m"
import { ERRNO_EPERM, ERRNO_ESRCH, ERRNO_EBADF, ERRNO_EPIPE, ERRNO_EFAULT,
    ERRNO_EBUSY, ERRNO_EMSGSIZE, ERRNO_EINVAL, ERRNO_EDEADLK, ERRNO_EOVERFLOW,
    ERRNO_ETIMEDOUT, ERRNO_ECANCELED, ERRNO_EAGAIN,
    IPC_MESSAGE_MAX, RIGHT_SEND, RIGHT_RECEIVE, PTE_R, PTE_W, TASK_SLOT_BITS, TASK_SLOT_MASK,
    TASK_GENERATION_MAX } from "../arch/wrm081632/defs.m"

let ipcCallerValid(): Bool {
    objectAssertAtomic()
    return currentTask != null && currentTask.id != 0 && currentTask.state == TASK_RUNNING
}

// Resolve authority before accessing an endpoint or its wait queues.
// The returned kernel pointer is borrowed only while IRQs remain excluded.
let ipcResolve(token: UWord, rights: UWord): *mut Endpoint {
    if !ipcCallerValid() || rights == 0 return null
    return handleLookup(&mut currentTask.handles, token, rights)
}

let ipcClose(token: UWord): Word {
    if !ipcCallerValid() return -ERRNO_EPERM
    return handleClose(&mut currentTask.handles, token)
}

// Ordinary copying spends only the caller's own table budget. Foreign tables
// require receiver-issued transfer consent or trusted unpublished-child setup.
let ipcCopy(token: UWord, targetId: UWord, rights: UWord): Word {
    if !ipcCallerValid() return -ERRNO_EPERM
    let target: *mut Task = taskGet(targetId)
    if target == null || (target.state != TASK_READY && target.state != TASK_RUNNING &&
        target.state != TASK_BLOCKED) return -ERRNO_ESRCH
    if target != currentTask return -ERRNO_EPERM
    return handleCopy(&mut currentTask.handles, token, &mut target.handles,
        currentTask.id, target.id, rights)
}

// A numeric receiver reference only selects an owned unpublished child.
let ipcCreate(mode: UWord, receiverReference: UWord): Word {
    let mut receiver: UWord = receiverReference
    if !ipcCallerValid() return -ERRNO_EPERM
    if currentTask.handles.factoryModes == 0 return -ERRNO_EPERM
    if mode > ENDPOINT_SERVICE return -ERRNO_EINVAL
    if receiver == 0 receiver = currentTask.id
    if mode == ENDPOINT_RAW && receiver != currentTask.id return -ERRNO_EINVAL
    if receiver != currentTask.id {
        if taskControlLookup(receiver, TASK_RIGHT_CONFIGURE) == null return -ERRNO_EPERM
        let child: *mut Task = taskGet(receiver)
        if child == null || child.state != TASK_CREATED || child.configured return -ERRNO_EBUSY
    }
    return endpointFactoryCreate(&mut currentTask.handles, currentTask.id, receiver, mode)
}

let ipcDestroy(token: UWord): Word {
    if !ipcCallerValid() return -ERRNO_EPERM
    return endpointDestroy(&mut currentTask.handles, token, currentTask.id)
}

// Transport results use r1=count or -errno, r2=verified/required count.
// All other GPRs/FCSR survive. Payload fields carry no authenticated identity.
let ipcResult(frame: *mut TrapFrame, result: Word, size: UWord): *TrapFrame {
    frame.regs[1] = result as UWord
    frame.regs[2] = size
    taskSaveContext(frame)
    return frame
}

let ipcAccess(token: UWord, rights: UWord): Word {
    let entry: *mut Handle = handleEntry(&mut currentTask.handles, token)
    if entry == null return -ERRNO_EBADF
    if entry.rights & rights != rights return -ERRNO_EPERM
    if handleLookup(&mut currentTask.handles, token, rights) == null return -ERRNO_EPIPE
    return 0
}

// FIFO entries contain generation-bearing task references. A wait owns an endpoint
// reference until detached; a TCB can belong to exactly one IPC queue.
let ipcWaiter(object: *mut Endpoint, kind: UWord): *mut Task {
    let mut id: UWord = 0
    if kind == WAIT_IPC_SEND || kind == WAIT_IPC_CALL {
        if object.senderCount == 0 return null
        id = object.senders[object.senderHead]
    } else {
        if object.receiverCount == 0 return null
        id = object.receivers[object.receiverHead]
    }
    let task: *mut Task = taskGet(id)
    if task == null || task.state != TASK_BLOCKED || task.queued ||
        task.ipcEndpoint != object || task.ipcKind != kind || task.waitReason != kind {
        panic("invalid IPC waiter", null)
        return null
    }
    return task
}

// Remove any position without disturbing FIFO order. Cancellation and delivery
// share this path, so each wait drops precisely one pin and clears its TCB.
let ipcUnqueue(task: *mut Task): Void {
    let object: *mut Endpoint = task.ipcEndpoint
    let sending: Bool = task.ipcKind == WAIT_IPC_SEND || task.ipcKind == WAIT_IPC_CALL
    let mut queue: *mut UWord = &mut object.receivers[0]
    let mut head: UWord = object.receiverHead
    let mut count: UWord = object.receiverCount
    if sending {
        queue = &mut object.senders[0]
        head = object.senderHead
        count = object.senderCount
    }
    let mut position: UWord = 0
    while position < count && queue[(head + position) % endpointWaitCapacity] != task.id position += 1
    if position == count || object.references == 0 {
        panic("missing IPC wait reference", null)
        return
    }
    let mut i: UWord = position
    while i + 1 < count {
        queue[(head + i) % endpointWaitCapacity] = queue[(head + i + 1) % endpointWaitCapacity]
        i += 1
    }
    queue[(head + count - 1) % endpointWaitCapacity] = 0
    if sending object.senderCount -= 1
    else object.receiverCount -= 1
}

let ipcClearMessage(task: *mut Task): Void {
    for j: UWord in 0..IPC_MESSAGE_MAX task.ipcMessage[j] = 0
}

let ipcDetach(task: *mut Task): Void {
    let object: *mut Endpoint = task.ipcEndpoint
    if task.ipcKind != WAIT_IPC_REPLY ipcUnqueue(task)
    task.ipcEndpoint = null
    task.ipcKind = WAIT_NONE
    task.ipcBuffer = 0
    task.ipcSize = 0
    task.ipcObjectGeneration = 0
    task.ipcReplyOwner = 0
    task.ipcReplyBuffer = 0
    task.ipcReplyCapacity = 0
    ipcClearMessage(task)
    object.references -= 1
    endpointRelease(object)
}

// Every terminal wait event uses this serialized transition. Death consumes
// the same record without publishing Ready; a consumed record cannot win again.
let ipcFinishWait(task: *mut Task, result: Word, size: UWord, wake: Bool): Bool {
    objectAssertAtomic()
    if task.state != TASK_BLOCKED || task.queued ||
        (task.ipcEndpoint == null && task.waitReason != WAIT_SLEEP) return false
    if task.ipcEndpoint != null ipcDetach(task)
    task.waitTimed = 0
    task.waitDeadline.lo = 0
    task.waitDeadline.hi = 0
    task.waitReason = WAIT_NONE
    task.context.regs[1] = result as UWord
    task.context.regs[2] = size
    if wake && !taskWake(task.id) panic("could not wake IPC waiter", null)
    return true
}

let ipcComplete(task: *mut Task, result: Word, size: UWord): Void {
    if !ipcFinishWait(task, result, size, true) panic("invalid IPC completion", null)
}

let ipcPublish(object: *mut Endpoint, kind: UWord, buffer: UWord, size: UWord): Word {
    let task: *mut Task = currentTask
    if task.ipcEndpoint != null || task.ipcKind != WAIT_NONE || task.queued {
        return -ERRNO_EBUSY
    }
    if kind == WAIT_IPC_SEND || kind == WAIT_IPC_CALL {
        if object.senderCount == endpointWaitCapacity return -ERRNO_EBUSY
        object.senders[(object.senderHead + object.senderCount) % endpointWaitCapacity] = task.id
        object.senderCount += 1
    } else {
        if object.receiverCount == endpointWaitCapacity return -ERRNO_EBUSY
        object.receivers[(object.receiverHead + object.receiverCount) % endpointWaitCapacity] = task.id
        object.receiverCount += 1
    }
    object.references += 1
    task.ipcEndpoint = object
    task.ipcObjectGeneration = object.generation
    task.ipcKind = kind
    task.ipcBuffer = buffer
    task.ipcSize = size
    return 0
}

let ipcWait(frame: *mut TrapFrame, object: *mut Endpoint, kind: UWord,
    buffer: UWord, size: UWord): *TrapFrame {
    let result: Word = ipcPublish(object, kind, buffer, size)
    if result != 0 return ipcResult(frame, result, 0)
    // Queue publication and Blocked happen in the same IRQ exclusion region.
    return taskBlock(frame, kind)
}

let ipcSendMode(frame: *mut TrapFrame, token: UWord, buffer: UWord, size: UWord, nonblocking: Bool): *TrapFrame {
    if !ipcCallerValid() || !taskOwnsTrap(frame) return ipcResult(frame, -ERRNO_EPERM, 0)
    if currentTask.ipcEndpoint != null || currentTask.ipcKind != WAIT_NONE || currentTask.queued {
        return ipcResult(frame, -ERRNO_EBUSY, 0)
    }
    let access: Word = ipcAccess(token, RIGHT_SEND)
    if access != 0 return ipcResult(frame, access, 0)
    if ipcResolve(token, RIGHT_SEND).mode != ENDPOINT_RAW return ipcResult(frame, -ERRNO_EINVAL, 0)
    if size > IPC_MESSAGE_MAX return ipcResult(frame, -ERRNO_EMSGSIZE, 0)
    // Full validation and snapshot finish before any endpoint queue mutation.
    let copied: Word = copyFromUser(currentTask.directory, currentTask.id,
        &mut currentTask.ipcMessage[0], buffer, size)
    if copied != 0 return ipcResult(frame, copied, 0)
    let object: *mut Endpoint = ipcResolve(token, RIGHT_SEND)
    while object.receiverCount != 0 {
        let receiver: *mut Task = ipcWaiter(object, WAIT_IPC_RECEIVE)
        if receiver.ipcSize < size {
            ipcComplete(receiver, -ERRNO_EMSGSIZE, size)
            continue
        }
        // Revalidate the entire capacity after sleeping, then copy through
        // the RECEIVER directory while the sender's PTBR remains active.
        if !mmuUserBufferValid(receiver.directory, receiver.id, receiver.ipcBuffer, receiver.ipcSize, PTE_W) {
            ipcComplete(receiver, -ERRNO_EFAULT, 0)
            continue
        }
        let result: Word = copyToUser(receiver.directory, receiver.id, receiver.ipcBuffer,
            &currentTask.ipcMessage[0], size)
        if result != 0 {
            ipcComplete(receiver, result, 0)
            continue
        }
        ipcComplete(receiver, size as Word, size)
        for i: UWord in 0..IPC_MESSAGE_MAX currentTask.ipcMessage[i] = 0
        return ipcResult(frame, size as Word, size)
    }
    if nonblocking {
        ipcClearMessage(currentTask)
        return ipcResult(frame, -ERRNO_EAGAIN, 0)
    }
    // The sender VA is discarded: only the kernel snapshot survives the wait.
    return ipcWait(frame, object, WAIT_IPC_SEND, 0, size)
}

let ipcReceiveMode(frame: *mut TrapFrame, token: UWord, buffer: UWord, capacity: UWord, nonblocking: Bool): *TrapFrame {
    if !ipcCallerValid() || !taskOwnsTrap(frame) return ipcResult(frame, -ERRNO_EPERM, 0)
    if currentTask.ipcEndpoint != null || currentTask.ipcKind != WAIT_NONE || currentTask.queued {
        return ipcResult(frame, -ERRNO_EBUSY, 0)
    }
    let access: Word = ipcAccess(token, RIGHT_RECEIVE)
    if access != 0 return ipcResult(frame, access, 0)
    if ipcResolve(token, RIGHT_RECEIVE).mode != ENDPOINT_RAW return ipcResult(frame, -ERRNO_EINVAL, 0)
    if capacity > IPC_MESSAGE_MAX return ipcResult(frame, -ERRNO_EMSGSIZE, 0)
    if !mmuUserBufferValid(currentTask.directory, currentTask.id, buffer, capacity, PTE_W) {
        return ipcResult(frame, -ERRNO_EFAULT, 0)
    }
    let object: *mut Endpoint = ipcResolve(token, RIGHT_RECEIVE)
    let sender: *mut Task = ipcWaiter(object, WAIT_IPC_SEND)
    if sender == null {
        if nonblocking return ipcResult(frame, -ERRNO_EAGAIN, 0)
        return ipcWait(frame, object, WAIT_IPC_RECEIVE, buffer, capacity)
    }
    let size: UWord = sender.ipcSize
    // No dequeue, wake or partial copy on insufficient capacity. A retry
    // observes the same oldest message, even if newer senders are waiting.
    if capacity < size return ipcResult(frame, -ERRNO_EMSGSIZE, size)
    let result: Word = copyToUser(currentTask.directory, currentTask.id, buffer,
        &sender.ipcMessage[0], size)
    if result != 0 return ipcResult(frame, result, 0)
    ipcComplete(sender, size as Word, size)
    return ipcResult(frame, size as Word, size)
}

// Service requests leave their FIFO without completing the client's wait.
// The endpoint pin spans AwaitAccept and AwaitReply; no Ready window exists.
let ipcAcceptCall(client: *mut Task, service: *mut Task, buffer: UWord, capacity: UWord): Word {
    let size: UWord = client.ipcSize
    if capacity < size return -ERRNO_EMSGSIZE
    if !mmuUserBufferValid(service.directory, service.id, buffer, capacity, PTE_W) return -ERRNO_EFAULT
    let result: Word = copyToUser(service.directory, service.id, buffer, &client.ipcMessage[0], size)
    if result != 0 return result
    ipcUnqueue(client)
    client.ipcKind = WAIT_IPC_REPLY
    client.waitReason = WAIT_IPC_REPLY
    client.ipcReplyOwner = service.id
    client.ipcSize = 0
    ipcClearMessage(client)
    return ((client.ipcCallGeneration << TASK_SLOT_BITS) | client.slot) as Word
}

let ipcCallMode(frame: *mut TrapFrame, token: UWord, buffer: UWord, size: UWord,
    response: UWord, capacity: UWord, seconds: UWord): *TrapFrame {
    if !ipcCallerValid() || !taskOwnsTrap(frame) return ipcResult(frame, -ERRNO_EPERM, 0)
    if currentTask.ipcEndpoint != null || currentTask.ipcKind != WAIT_NONE || currentTask.queued {
        return ipcResult(frame, -ERRNO_EBUSY, 0)
    }
    let access: Word = ipcAccess(token, RIGHT_SEND)
    if access != 0 return ipcResult(frame, access, 0)
    let object: *mut Endpoint = ipcResolve(token, RIGHT_SEND)
    if object.mode != ENDPOINT_SERVICE return ipcResult(frame, -ERRNO_EINVAL, 0)
    let service: *mut Task = taskGet(object.manager)
    if service == null || (service.state != TASK_READY && service.state != TASK_RUNNING &&
        service.state != TASK_BLOCKED) return ipcResult(frame, -ERRNO_EPIPE, 0)
    if service == currentTask return ipcResult(frame, -ERRNO_EDEADLK, 0)
    if size > IPC_MESSAGE_MAX || capacity > IPC_MESSAGE_MAX return ipcResult(frame, -ERRNO_EMSGSIZE, 0)
    if !mmuUserBufferValid(currentTask.directory, currentTask.id, buffer, size, PTE_R) ||
        !mmuUserBufferValid(currentTask.directory, currentTask.id, response, capacity, PTE_W) {
        return ipcResult(frame, -ERRNO_EFAULT, 0)
    }
    if currentTask.ipcCallGeneration == TASK_GENERATION_MAX return ipcResult(frame, -ERRNO_EOVERFLOW, 0)
    if object.senderCount == endpointWaitCapacity return ipcResult(frame, -ERRNO_EBUSY, 0)
    let mut deadline: TimerCount
    if seconds != 0 && !timerDeadline(seconds, &mut deadline) return ipcResult(frame, -ERRNO_EINVAL, 0)
    let copied: Word = copyFromUser(currentTask.directory, currentTask.id, &mut currentTask.ipcMessage[0], buffer, size)
    if copied != 0 return ipcResult(frame, copied, 0)
    let published: Word = ipcPublish(object, WAIT_IPC_CALL, 0, size)
    if published != 0 {
        ipcClearMessage(currentTask)
        return ipcResult(frame, published, 0)
    }
    if seconds != 0 {
        currentTask.waitTimed = 1
        currentTask.waitDeadline = deadline
    }
    currentTask.ipcCallGeneration += 1
    currentTask.ipcReplyBuffer = response
    currentTask.ipcReplyCapacity = capacity
    if object.receiverCount != 0 {
        let receiver: *mut Task = ipcWaiter(object, WAIT_IPC_ACCEPT)
        let accepted: Word = ipcAcceptCall(currentTask, receiver, receiver.ipcBuffer, receiver.ipcSize)
        if accepted < 0 {
            let mut required: UWord = 0
            if accepted == -ERRNO_EMSGSIZE required = size
            ipcComplete(receiver, accepted, required)
        } else ipcComplete(receiver, size as Word, accepted as UWord)
    }
    return taskBlock(frame, currentTask.ipcKind)
}

let ipcAcceptMode(frame: *mut TrapFrame, token: UWord, buffer: UWord, capacity: UWord, nonblocking: Bool): *TrapFrame {
    if !ipcCallerValid() || !taskOwnsTrap(frame) return ipcResult(frame, -ERRNO_EPERM, 0)
    if currentTask.ipcEndpoint != null || currentTask.ipcKind != WAIT_NONE || currentTask.queued {
        return ipcResult(frame, -ERRNO_EBUSY, 0)
    }
    let access: Word = ipcAccess(token, RIGHT_RECEIVE)
    if access != 0 return ipcResult(frame, access, 0)
    let object: *mut Endpoint = ipcResolve(token, RIGHT_RECEIVE)
    if object.mode != ENDPOINT_SERVICE return ipcResult(frame, -ERRNO_EINVAL, 0)
    if object.manager != currentTask.id return ipcResult(frame, -ERRNO_EPERM, 0)
    if capacity > IPC_MESSAGE_MAX return ipcResult(frame, -ERRNO_EMSGSIZE, 0)
    if !mmuUserBufferValid(currentTask.directory, currentTask.id, buffer, capacity, PTE_W) {
        return ipcResult(frame, -ERRNO_EFAULT, 0)
    }
    let client: *mut Task = ipcWaiter(object, WAIT_IPC_CALL)
    if client == null {
        if nonblocking return ipcResult(frame, -ERRNO_EAGAIN, 0)
        return ipcWait(frame, object, WAIT_IPC_ACCEPT, buffer, capacity)
    }
    let size: UWord = client.ipcSize
    let result: Word = ipcAcceptCall(client, currentTask, buffer, capacity)
    if result < 0 {
        let mut required: UWord = 0
        if result == -ERRNO_EMSGSIZE required = size
        return ipcResult(frame, result, required)
    }
    return ipcResult(frame, size as Word, result as UWord)
}

let ipcReply(frame: *mut TrapFrame, token: UWord, buffer: UWord, size: UWord): *TrapFrame {
    if !ipcCallerValid() || !taskOwnsTrap(frame) return ipcResult(frame, -ERRNO_EPERM, 0)
    // A numeric token selects a record, but authority is its kernel-owned owner.
    let client: *mut Task = taskSlot(token & TASK_SLOT_MASK)
    if token >> TASK_SLOT_BITS == 0 || token >> TASK_SLOT_BITS > TASK_GENERATION_MAX || client == null {
        return ipcResult(frame, -ERRNO_EBADF, 0)
    }
    if client.state != TASK_BLOCKED || client.queued || client.ipcKind != WAIT_IPC_REPLY ||
        client.waitReason != WAIT_IPC_REPLY || client.ipcCallGeneration != token >> TASK_SLOT_BITS ||
        client.ipcReplyOwner != currentTask.id || client.ipcEndpoint == null {
        return ipcResult(frame, -ERRNO_EBADF, 0)
    }
    let object: *mut Endpoint = client.ipcEndpoint
    if object.mode != ENDPOINT_SERVICE || object.state != ENDPOINT_LIVE ||
        object.generation != client.ipcObjectGeneration || object.manager != currentTask.id {
        return ipcResult(frame, -ERRNO_EBADF, 0)
    }
    if size > IPC_MESSAGE_MAX return ipcResult(frame, -ERRNO_EMSGSIZE, 0)
    let copied: Word = copyFromUser(currentTask.directory, currentTask.id, &mut currentTask.ipcMessage[0], buffer, size)
    if copied != 0 return ipcResult(frame, copied, 0)
    let mut result: Word = size as Word
    let mut required: UWord = size
    if size > client.ipcReplyCapacity result = -ERRNO_EMSGSIZE
    else if !mmuUserBufferValid(client.directory, client.id, client.ipcReplyBuffer, client.ipcReplyCapacity, PTE_W) {
        result = -ERRNO_EFAULT
        required = 0
    } else {
        let delivered: Word = copyToUser(client.directory, client.id, client.ipcReplyBuffer, &currentTask.ipcMessage[0], size)
        if delivered != 0 {
            result = delivered
            required = 0
        }
    }
    ipcClearMessage(currentTask)
    ipcComplete(client, result, required)
    return ipcResult(frame, result, required)
}

// Revocation invalidates all outstanding calls, in sender/receiver FIFO order.
// Mark the endpoint Destroyed before entering this routine.
let ipcCancelEndpoint(object: *mut Endpoint): Void {
    objectAssertAtomic()
    let mut sending: UWord = WAIT_IPC_SEND
    let mut receiving: UWord = WAIT_IPC_RECEIVE
    if object.mode == ENDPOINT_SERVICE {
        sending = WAIT_IPC_CALL
        receiving = WAIT_IPC_ACCEPT
    }
    while object.senderCount != 0 ipcComplete(ipcWaiter(object, sending), -ERRNO_EPIPE, 0)
    while object.receiverCount != 0 ipcComplete(ipcWaiter(object, receiving), -ERRNO_EPIPE, 0)
    for id: UWord in 1..(taskHighWater + 1) {
        let task: *mut Task = taskSlot(id)
        if task.ipcEndpoint == object && task.ipcKind == WAIT_IPC_REPLY ipcComplete(task, -ERRNO_EPIPE, 0)
    }
}

// Called before a task loses its handles, pages or TCB. There is no paired
// peer before rendezvous; unrelated same-side waiters keep their FIFO places.
let ipcCancelTask(id: UWord): Void {
    objectAssertAtomic()
    let task: *mut Task = taskGet(id)
    if task == null || (task.ipcEndpoint == null && task.waitReason != WAIT_SLEEP) return
    if task.state != TASK_BLOCKED || task.queued {
        panic("cancel of active IPC waiter", null)
        return
    }
    if !ipcFinishWait(task, -ERRNO_ECANCELED, 0, false) panic("invalid dying waiter", null)
}

export { ipcCallerValid, ipcCreate, ipcResolve, ipcClose, ipcCopy, ipcDestroy, ipcSend, ipcReceive,
    ipcCall, ipcAccept, ipcReply,
    ipcCancelEndpoint, ipcCancelTask }

let ipcSend(frame: *mut TrapFrame, token: UWord, buffer: UWord, size: UWord): *TrapFrame {
    return ipcSendMode(frame, token, buffer, size, false)
}
let ipcReceive(frame: *mut TrapFrame, token: UWord, buffer: UWord, capacity: UWord): *TrapFrame {
    return ipcReceiveMode(frame, token, buffer, capacity, false)
}
let ipcAccept(frame: *mut TrapFrame, token: UWord, buffer: UWord, capacity: UWord): *TrapFrame {
    return ipcAcceptMode(frame, token, buffer, capacity, false)
}
let ipcCall(frame: *mut TrapFrame, token: UWord, buffer: UWord, size: UWord,
    response: UWord, capacity: UWord): *TrapFrame {
    return ipcCallMode(frame, token, buffer, size, response, capacity, 0)
}
let ipcCallTimed(frame: *mut TrapFrame, token: UWord, buffer: UWord, size: UWord,
    response: UWord, capacity: UWord, seconds: UWord): *TrapFrame {
    if seconds == 0 return ipcResult(frame, -ERRNO_EINVAL, 0)
    return ipcCallMode(frame, token, buffer, size, response, capacity, seconds)
}

// Cancellation addresses the current synchronous wait under scoped authority.
// Task references bear generations; they never confer authority by themselves.
let ipcSupervisorCancel(reference: UWord): Word {
    if !ipcCallerValid() || taskControlLookup(reference, TASK_RIGHT_CANCEL) == null return -ERRNO_EPERM
    let task: *mut Task = taskGet(reference)
    if task == null || (task.state != TASK_READY && task.state != TASK_RUNNING &&
        task.state != TASK_BLOCKED) return -ERRNO_ESRCH
    if !ipcFinishWait(task, -ERRNO_ECANCELED, 0, true) return -ERRNO_EAGAIN
    return 0
}

let ipcSleep(frame: *mut TrapFrame, seconds: UWord): *TrapFrame {
    if !ipcCallerValid() || !taskOwnsTrap(frame) return ipcResult(frame, -ERRNO_EPERM, 0)
    if currentTask.ipcEndpoint != null || currentTask.waitTimed != 0 return ipcResult(frame, -ERRNO_EBUSY, 0)
    let mut deadline: TimerCount
    if !timerDeadline(seconds, &mut deadline) return ipcResult(frame, -ERRNO_EINVAL, 0)
    currentTask.waitDeadline = deadline
    currentTask.waitTimed = 1
    return taskBlock(frame, WAIT_SLEEP)
}

// At most one record per slot ever used and bounded FIFO/message cleanup per IRQ. COUNT,
// not the number of delivered/coalesced IRQs, determines expiry.
let ipcTimerTick(): Void {
    objectAssertAtomic()
    transferTimerTick()
    let mut hasWait: Bool = false
    for slot: UWord in 1..(taskHighWater + 1) {
        if taskSlot(slot).waitTimed != 0 hasWait = true
    }
    if !hasWait return
    let mut now: TimerCount
    timerReadCount(&mut now)
    for slot: UWord in 1..(taskHighWater + 1) {
        let task: *mut Task = taskSlot(slot)
        if task.state != TASK_BLOCKED || task.waitTimed == 0 ||
            !timerDeadlineReached(&now, &task.waitDeadline) continue
        let mut result: Word = -ERRNO_ETIMEDOUT
        if task.waitReason == WAIT_SLEEP result = 0
        ipcComplete(task, result, 0)
    }
}
export { ipcCallTimed, ipcSendMode, ipcReceiveMode, ipcAcceptMode,
    ipcSupervisorCancel, ipcSleep, ipcTimerTick }

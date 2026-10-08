// Trusted bootstrap issues one receive capability and attenuated send rights.
import { Task, taskGet, taskInstallStart } from "../../../src/task/task.m"
import { TaskStart } from "../../../src/task/start.m"
import { endpointBootstrapService, handleCopy, handleClose } from "../../../src/ipc/objects.m"
import { RIGHT_RECEIVE, RIGHT_SEND, START_MAGIC, START_VERSION, START_BLOCK_BYTES, START_DATA_VA,
    START_ROLE_SERVER, START_PROTOCOL_CONSOLE, DEVICE_UART_TX, IPC_MESSAGE_MAX, PAGE_SIZE,
    START_HANDLES_MAGIC, START_HANDLES_MAX } from "../../../src/arch/wrm081632/defs.m"

let serviceEndpoint(receiver: UWord, sender: UWord, receive: *mut UWord, send: *mut UWord): Bool {
    let server: *mut Task = taskGet(receiver)
    let client: *mut Task = taskGet(sender)
    let root: Word = endpointBootstrapService(&mut server.handles, receiver)
    if root < 0 return false
    let rx: Word = handleCopy(&mut server.handles, root as UWord,
        &mut server.handles, receiver, receiver, RIGHT_RECEIVE)
    if rx < 0 return false
    let tx: Word = handleCopy(&mut server.handles, root as UWord,
        &mut client.handles, receiver, sender, RIGHT_SEND)
    if tx < 0 return false
    if handleClose(&mut server.handles, root as UWord) != 0 return false
    receive[0] = rx as UWord
    send[0] = tx as UWord
    return true
}

// One receive capability and an attenuated send right for each of up to four
// senders, all on the same endpoint, so a server with several clients accepts
// from one place. Nothing is left behind if a copy fails: the caller discards
// the created tasks, which releases every handle.
let serviceEndpointMulti(receiver: UWord, senders: *UWord, count: UWord, receive: *mut UWord, sends: *mut UWord): Bool {
    if count == 0 || count > 4 return false
    let server: *mut Task = taskGet(receiver)
    if server == null return false
    let root: Word = endpointBootstrapService(&mut server.handles, receiver)
    if root < 0 return false
    let rx: Word = handleCopy(&mut server.handles, root as UWord,
        &mut server.handles, receiver, receiver, RIGHT_RECEIVE)
    if rx < 0 return false
    for i: UWord in 0..count {
        let client: *mut Task = taskGet(senders[i])
        if client == null return false
        let tx: Word = handleCopy(&mut server.handles, root as UWord,
            &mut client.handles, receiver, senders[i], RIGHT_SEND)
        if tx < 0 return false
        sends[i] = tx as UWord
    }
    if handleClose(&mut server.handles, root as UWord) != 0 return false
    receive[0] = rx as UWord
    return true
}

// The legacy console server (bootstrap.asm): UART transmit authority and the
// checked console protocol, on the given receive handle.
let serviceConsoleStart(id: UWord, endpoint: UWord): Bool {
    let mut block: TaskStart
    block.magic = START_MAGIC
    block.version = START_VERSION
    block.bytes = START_BLOCK_BYTES
    block.role = START_ROLE_SERVER
    block.taskId = id
    block.endpoint = endpoint
    block.rights = RIGHT_RECEIVE
    block.devices = DEVICE_UART_TX
    block.data = START_DATA_VA
    block.dataBytes = PAGE_SIZE
    block.ipcLimit = IPC_MESSAGE_MAX
    block.protocol = START_PROTOCOL_CONSOLE
    return taskInstallStart(id, &block)
}

// Writes the start handle list into the data page of an unpublished task (the
// handles beyond its one start endpoint; see user/starthandles.m).
let serviceHandles(id: UWord, handles: *UWord, count: UWord): Bool {
    let task: *mut Task = taskGet(id)
    if task == null || count > START_HANDLES_MAX return false
    let page: *mut UWord = task.pages[1] as *mut UWord
    page[0] = START_HANDLES_MAGIC
    page[1] = count
    for i: UWord in 0..START_HANDLES_MAX {
        if i < count page[2 + i] = handles[i]
        else page[2 + i] = 0
    }
    return true
}

export { serviceEndpoint, serviceEndpointMulti, serviceConsoleStart, serviceHandles }

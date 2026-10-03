// Trusted init policy embedded in the boot image. No user loader, registry,
// arbitrary task-control syscall or authority inferred from user parameters.
import { taskInitAvailable, taskCreateImage, taskGet, taskInstallStart,
    taskPublish, taskDiscardCreated, Task } from "../task/task.m"
import { TaskStart } from "../task/start.m"
import { endpointBootstrapService, handleCopy, handleClose } from "../ipc/objects.m"
import { panic } from "panic.m"
import { START_MAGIC, START_VERSION, START_BLOCK_BYTES, START_DATA_VA,
    START_ROLE_SERVER, START_ROLE_CLIENT, START_PROTOCOL_CONSOLE,
    RIGHT_RECEIVE, RIGHT_SEND, DEVICE_UART_TX, PAGE_SIZE, IPC_MESSAGE_MAX } from "../arch/wrm081632/defs.m"

extern let bootstrapServerStart: UByte
extern let bootstrapServerEnd: UByte
extern let bootstrapClientStart: UByte
extern let bootstrapClientEnd: UByte

// Two immutable resource rows: role, endpoint rights, narrow device rights.
// Image IDs are fixed by init, never chosen by an application.
let bootstrapResources: UWord[6] = [START_ROLE_SERVER, RIGHT_RECEIVE, DEVICE_UART_TX,
    START_ROLE_CLIENT, RIGHT_SEND, 0]

let bootstrapInstall(id: UWord, row: UWord, token: UWord): Bool {
    if row >= 2 return false
    let mut block: TaskStart
    block.magic = START_MAGIC
    block.version = START_VERSION
    block.bytes = START_BLOCK_BYTES
    block.role = bootstrapResources[row * 3]
    block.taskId = id
    block.endpoint = token
    block.rights = bootstrapResources[row * 3 + 1]
    block.devices = bootstrapResources[row * 3 + 2]
    block.data = START_DATA_VA
    block.dataBytes = PAGE_SIZE
    block.ipcLimit = IPC_MESSAGE_MAX
    block.protocol = START_PROTOCOL_CONSOLE
    return taskInstallStart(id, &block)
}

let bootstrapRollback(server: UWord, client: UWord): Bool {
    if client != 0 && !taskDiscardCreated(client) panic("could not discard bootstrap client", null)
    if server != 0 && !taskDiscardCreated(server) panic("could not discard bootstrap server", null)
    return false
}

let bootstrapInit(): Bool {
    if !taskInitAvailable() return false
    let server: UWord = taskCreateImage(&bootstrapServerStart as UWord, &bootstrapServerEnd as UWord, 0)
    if server == 0 return false
    let client: UWord = taskCreateImage(&bootstrapClientStart as UWord, &bootstrapClientEnd as UWord, 0)
    if client == 0 return bootstrapRollback(server, 0)
    let receiverTask: *mut Task = taskGet(server)
    let senderTask: *mut Task = taskGet(client)
    // The temporary root handle never reaches user mode. Management remains
    // bound to the server's lifetime; its sole receive handle revokes on close.
    let root: Word = endpointBootstrapService(&mut receiverTask.handles, server)
    if root < 0 return bootstrapRollback(server, client)
    let receive: Word = handleCopy(&mut receiverTask.handles, root as UWord,
        &mut receiverTask.handles, server, server, RIGHT_RECEIVE)
    if receive < 0 return bootstrapRollback(server, client)
    let send: Word = handleCopy(&mut receiverTask.handles, root as UWord,
        &mut senderTask.handles, server, client, RIGHT_SEND)
    if send < 0 return bootstrapRollback(server, client)
    if handleClose(&mut receiverTask.handles, root as UWord) != 0 ||
        !bootstrapInstall(server, 0, receive as UWord) ||
        !bootstrapInstall(client, 1, send as UWord) return bootstrapRollback(server, client)
    // All fallible allocations/grants precede publication. IRQs are disabled,
    // so both checked Created records publish together before any user entry.
    if !taskPublish(server) || !taskPublish(client) {
        panic("invalid bootstrap publication", null)
        return false
    }
    return true
}

export { bootstrapInit }

// Shared, pointer-free user entry record. It is copied by trusted init, then
// mapped read-only and non-executable in the receiving task's private root.
import { START_MAGIC, START_VERSION, START_BLOCK_BYTES, START_DATA_VA,
    START_ROLE_SERVER, START_ROLE_CLIENT, START_PROTOCOL_CONSOLE,
    RIGHT_RECEIVE, RIGHT_SEND, DEVICE_UART_TX, PAGE_SIZE, IPC_MESSAGE_MAX } from "../arch/wrm081632/defs.m"

type TaskStart {
    magic: UWord,
    version: UWord,
    bytes: UWord,
    role: UWord,
    taskId: UWord,
    endpoint: UWord,
    rights: UWord,
    devices: UWord,
    data: UWord,
    dataBytes: UWord,
    ipcLimit: UWord,
    protocol: UWord,
}

let taskStartBlockValid(block: *TaskStart): Bool {
    if block == null || sizeof(TaskStart) != START_BLOCK_BYTES ||
        block.magic != START_MAGIC || block.version != START_VERSION ||
        block.bytes != START_BLOCK_BYTES || block.taskId == 0 || block.endpoint == 0 ||
        block.data != START_DATA_VA || block.dataBytes != PAGE_SIZE ||
        block.ipcLimit != IPC_MESSAGE_MAX || block.protocol != START_PROTOCOL_CONSOLE return false
    return (block.role == START_ROLE_SERVER && block.rights == RIGHT_RECEIVE && block.devices == DEVICE_UART_TX) ||
        (block.role == START_ROLE_CLIENT && block.rights == RIGHT_SEND && block.devices == 0)
}

export { TaskStart, taskStartBlockValid }

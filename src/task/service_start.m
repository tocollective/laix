// Startup ABI for separately embedded user services. Legacy UART v1
// records retain their original size and validation rules.
import { START_MAGIC, SERVICE_START_VERSION, SERVICE_START_BYTES,
    START_DATA_VA, PAGE_SIZE, IPC_MESSAGE_MAX, START_ROLE_SERVER,
    START_ROLE_CLIENT, START_ROLE_STORAGE, START_PROTOCOL_SCREEN,
    START_PROTOCOL_FONT, RIGHT_RECEIVE, RIGHT_SEND, DEVICE_SCREEN,
    DEVICE_FONT, SCREEN_FONT_VA, DEVICE_INPUT, DEVICE_DISK,
    START_ROLE_INPUT, START_ROLE_DISK, START_ROLE_FILE,
    START_PROTOCOL_INPUT, START_PROTOCOL_DISK, START_PROTOCOL_FILE } from "../arch/wrm081632/defs.m"

type ServiceStart {
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
    bitmapEndpoint: UWord,
    fontIndex: UWord,
    fontBytes: UWord,
    irq: UWord,
}

let serviceStartValid(block: *ServiceStart): Bool {
    if block == null || sizeof(ServiceStart) != SERVICE_START_BYTES ||
        block.magic != START_MAGIC || block.version != SERVICE_START_VERSION ||
        block.bytes != SERVICE_START_BYTES || block.taskId == 0 || block.endpoint == 0 ||
        block.data != START_DATA_VA || block.dataBytes != PAGE_SIZE ||
        block.ipcLimit != IPC_MESSAGE_MAX return false
    if block.role == START_ROLE_SERVER return block.protocol == START_PROTOCOL_SCREEN &&
        block.rights == RIGHT_RECEIVE && block.devices == DEVICE_SCREEN &&
        block.bitmapEndpoint != 0 && block.fontIndex == SCREEN_FONT_VA && block.fontBytes != 0 && block.irq != 0
    if block.fontIndex != 0 || block.fontBytes != 0 return false
    if block.role == START_ROLE_FILE return block.protocol == START_PROTOCOL_FILE &&
        block.rights == RIGHT_RECEIVE && block.devices == 0 && block.irq == 0 && block.bitmapEndpoint != 0
    if block.role == START_ROLE_CLIENT && block.protocol == START_PROTOCOL_FILE return
        block.rights == RIGHT_SEND && block.devices == 0 && block.irq == 0 && block.bitmapEndpoint != 0
    if block.bitmapEndpoint != 0 return false
    if block.role == START_ROLE_INPUT return block.protocol == START_PROTOCOL_INPUT &&
        block.rights == RIGHT_RECEIVE && block.devices == DEVICE_INPUT && block.irq != 0
    if block.role == START_ROLE_DISK return block.protocol == START_PROTOCOL_DISK &&
        block.rights == RIGHT_RECEIVE && block.devices == DEVICE_DISK && block.irq != 0
    return (block.role == START_ROLE_STORAGE && block.protocol == START_PROTOCOL_FONT &&
        block.rights == RIGHT_RECEIVE && block.devices == DEVICE_FONT && block.irq != 0) ||
        (block.role == START_ROLE_CLIENT && block.protocol == START_PROTOCOL_SCREEN &&
        block.rights == RIGHT_SEND && block.devices == 0 && block.irq == 0)
}

export { ServiceStart, serviceStartValid }

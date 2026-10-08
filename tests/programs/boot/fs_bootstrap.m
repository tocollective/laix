// Input, Disk, the writable filesystem (Fs) and one acceptance client, as separate
// user services: the loader policy with Files replaced by Fs and the client by the
// filesystem acceptance client (G7). The kernel's service-start check still gives
// every file-protocol client an Input endpoint, so Input stays. Disk owns the
// whole approved storage root and, unlike every other profile, as a writable
// extent: boot policy asks for it, and it needs the root's writable bit.
import { taskCreateProgram } from "../../../src/task/program.m"
import { taskInitAvailable, taskInstallServiceStart,
    taskPublish, taskDiscardCreated } from "../../../src/task/task.m"
import { ServiceStart } from "../../../src/task/service_start.m"
import { serviceEndpoint } from "service_policy.m"
import { irqGrant } from "../../../src/drivers/irq.m"
import { DEVICE_ROLE_INPUT, deviceRoleIrq } from "../../../src/drivers/device_table.m"
import { inputDevicesInit } from "../../../src/drivers/input_device.m"
import { diskDevicesInitWritable, serviceDevicesRollback, serviceDiskIrq } from "../../../src/drivers/service_devices.m"
import { kernelBootInfo } from "../../../src/kernel/boot.m"
import { panic } from "../../../src/kernel/panic.m"
import { START_MAGIC, SERVICE_START_VERSION, SERVICE_START_BYTES, START_DATA_VA,
    START_ROLE_INPUT, START_ROLE_DISK, START_ROLE_FILE, START_ROLE_CLIENT,
    START_PROTOCOL_INPUT, START_PROTOCOL_DISK, START_PROTOCOL_FILE, DEVICE_INPUT, DEVICE_DISK,
    RIGHT_RECEIVE, RIGHT_SEND,
    IPC_MESSAGE_MAX, PAGE_SIZE } from "../../../src/arch/wrm081632/defs.m"
extern let inputImage: UByte
extern let inputImageEnd: UByte
extern let diskImage: UByte
extern let diskImageEnd: UByte
extern let fsImage: UByte
extern let fsImageEnd: UByte
extern let fsClientImage: UByte
extern let fsClientImageEnd: UByte

let fsRollback(ids: *UWord): Bool {
    for i: UWord in 0..4 {
        if ids[i] != 0 && !taskDiscardCreated(ids[i]) panic("could not discard filesystem service", null)
    }
    serviceDevicesRollback()
    return false
}

let fsInstall(id: UWord, role: UWord, endpoint: UWord, upstream: UWord, irq: UWord): Bool {
    let mut block: ServiceStart
    block.magic = START_MAGIC
    block.version = SERVICE_START_VERSION
    block.bytes = SERVICE_START_BYTES
    block.role = role
    block.taskId = id
    block.endpoint = endpoint
    block.rights = RIGHT_RECEIVE
    block.devices = 0
    block.data = START_DATA_VA
    block.dataBytes = PAGE_SIZE
    block.ipcLimit = IPC_MESSAGE_MAX
    block.protocol = START_PROTOCOL_FILE
    block.bitmapEndpoint = upstream
    block.fontIndex = 0
    block.fontBytes = 0
    block.irq = irq
    if role == START_ROLE_INPUT {
        block.devices = DEVICE_INPUT
        block.protocol = START_PROTOCOL_INPUT
    } else if role == START_ROLE_DISK {
        block.devices = DEVICE_DISK
        block.protocol = START_PROTOCOL_DISK
    } else if role == START_ROLE_CLIENT block.rights = RIGHT_SEND
    return taskInstallServiceStart(id, &block, serviceDiskIrq(kernelBootInfo.disk))
}

let bootstrapFsInit(): Bool {
    if !taskInitAvailable() return false
    let mut ids: UWord[4]
    for i: UWord in 0..4 ids[i] = 0
    ids[0] = taskCreateProgram(&inputImage as UWord, &inputImageEnd as UWord)
    if ids[0] == 0 return false
    ids[1] = taskCreateProgram(&diskImage as UWord, &diskImageEnd as UWord)
    if ids[1] == 0 return fsRollback(&ids[0])
    ids[2] = taskCreateProgram(&fsImage as UWord, &fsImageEnd as UWord)
    if ids[2] == 0 return fsRollback(&ids[0])
    ids[3] = taskCreateProgram(&fsClientImage as UWord, &fsClientImageEnd as UWord)
    if ids[3] == 0 return fsRollback(&ids[0])
    let mut inputReceive: UWord = 0
    let mut inputSend: UWord = 0
    let mut diskReceive: UWord = 0
    let mut diskSend: UWord = 0
    let mut fileReceive: UWord = 0
    let mut fileSend: UWord = 0
    if !serviceEndpoint(ids[0], ids[3], &mut inputReceive, &mut inputSend) ||
        !serviceEndpoint(ids[1], ids[2], &mut diskReceive, &mut diskSend) ||
        !serviceEndpoint(ids[2], ids[3], &mut fileReceive, &mut fileSend) return fsRollback(&ids[0])
    let inputIrq: UWord = irqGrant(ids[0], deviceRoleIrq(DEVICE_ROLE_INPUT))
    let diskIrq: UWord = irqGrant(ids[1], serviceDiskIrq(kernelBootInfo.disk))
    if inputIrq == 0 || diskIrq == 0 || !inputDevicesInit(ids[0], inputIrq) ||
        !diskDevicesInitWritable(ids[1], kernelBootInfo.disk, kernelBootInfo.imageSize) ||
        !fsInstall(ids[0], START_ROLE_INPUT, inputReceive, 0, inputIrq) ||
        !fsInstall(ids[1], START_ROLE_DISK, diskReceive, 0, diskIrq) ||
        !fsInstall(ids[2], START_ROLE_FILE, fileReceive, diskSend, 0) ||
        !fsInstall(ids[3], START_ROLE_CLIENT, fileSend, inputSend, 0) return fsRollback(&ids[0])
    for i: UWord in 0..4 {
        if !taskPublish(ids[i]) {
            panic("invalid filesystem service publication", null)
            return false
        }
    }
    return true
}
export { bootstrapFsInit }

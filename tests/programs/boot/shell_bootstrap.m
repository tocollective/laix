// Disk, the filesystem, the console server, Exec and the shell, as separate tasks
// (docs/SHELL.md). Five tasks leave one slot for a program Exec loads, because
// runtime creators may not use the two slots reserved for recovery. The shell
// owns the keyboard (raw Input broker) itself: a separate Input service would take
// a sixth slot and there is one reader.
//
//   Disk  ---- Fs ----+---- Exec ---- (programs: console only)
//                     |      |
//                     +---- Shell ---- keyboard
//   Console server <--+------+---- Shell, Exec (and the programs)
//
// Disk owns the whole storage root as a writable extent; no one else touches a
// device. Exec holds the image load authority and nothing else. Handles beyond a
// start record's single endpoint go in the start handle list of the task's data
// page: Exec [Fs, console]; Shell [Fs, Exec, console].
import { taskCreateProgram } from "../../../src/task/program.m"
import { taskCreateImage, taskInitAvailable, taskInstallServiceStart,
    taskGet, Task, taskPublish, taskDiscardCreated } from "../../../src/task/task.m"
import { taskInstallRuntimeStart } from "../../../src/task/control.m"
import { ServiceStart } from "../../../src/task/service_start.m"
import { serviceEndpoint, serviceEndpointMulti, serviceConsoleStart, serviceHandles } from "service_policy.m"
import { irqGrant } from "../../../src/drivers/irq.m"
import { DEVICE_ROLE_INPUT, deviceRoleIrq } from "../../../src/drivers/device_table.m"
import { inputDevicesInit } from "../../../src/drivers/input_device.m"
import { diskDevicesInitWritable, serviceDevicesRollback, serviceDiskIrq } from "../../../src/drivers/service_devices.m"
import { kernelBootInfo } from "../../../src/kernel/boot.m"
import { panic } from "../../../src/kernel/panic.m"
import { START_MAGIC, SERVICE_START_VERSION, SERVICE_START_BYTES,
    START_DATA_VA, START_ROLE_DISK, START_ROLE_FILE, START_PROTOCOL_DISK, START_PROTOCOL_FILE,
    DEVICE_DISK, DEVICE_INPUT, RIGHT_RECEIVE, IPC_MESSAGE_MAX, PAGE_SIZE,
    IMAGE_LOAD_AUTHORITY } from "../../../src/arch/wrm081632/defs.m"
extern let diskImage: UByte
extern let diskImageEnd: UByte
extern let fsImage: UByte
extern let fsImageEnd: UByte
extern let execImage: UByte
extern let execImageEnd: UByte
extern let shellImage: UByte
extern let shellImageEnd: UByte
extern let bootstrapServerStart: UByte
extern let bootstrapServerEnd: UByte

let SHELL_TASKS: UWord = 5 // disk, fs, console, exec, shell

let shellRollback(ids: *UWord): Bool {
    for i: UWord in 0..SHELL_TASKS {
        if ids[i] != 0 && !taskDiscardCreated(ids[i]) panic("could not discard shell service", null)
    }
    serviceDevicesRollback()
    return false
}

let shellServiceStart(id: UWord, role: UWord, endpoint: UWord, upstream: UWord, irq: UWord): Bool {
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
    if role == START_ROLE_DISK {
        block.devices = DEVICE_DISK
        block.protocol = START_PROTOCOL_DISK
    }
    return taskInstallServiceStart(id, &block, serviceDiskIrq(kernelBootInfo.disk))
}

let bootstrapShellInit(): Bool {
    if !taskInitAvailable() return false
    let mut ids: UWord[5]
    for i: UWord in 0..SHELL_TASKS ids[i] = 0
    ids[0] = taskCreateProgram(&diskImage as UWord, &diskImageEnd as UWord)
    if ids[0] == 0 return false
    ids[1] = taskCreateProgram(&fsImage as UWord, &fsImageEnd as UWord)
    if ids[1] == 0 return shellRollback(&ids[0])
    ids[2] = taskCreateImage(&bootstrapServerStart as UWord, &bootstrapServerEnd as UWord, 0)
    if ids[2] == 0 return shellRollback(&ids[0])
    ids[3] = taskCreateProgram(&execImage as UWord, &execImageEnd as UWord)
    if ids[3] == 0 return shellRollback(&ids[0])
    ids[4] = taskCreateProgram(&shellImage as UWord, &shellImageEnd as UWord)
    if ids[4] == 0 return shellRollback(&ids[0])
    let mut diskReceive: UWord = 0
    let mut diskSend: UWord = 0
    let mut fsReceive: UWord = 0
    let mut fsSend: UWord[2]
    let mut consoleReceive: UWord = 0
    let mut consoleSend: UWord[2]
    let mut execReceive: UWord = 0
    let mut execSend: UWord = 0
    let mut fsClients: UWord[2]
    let mut consoleClients: UWord[2]
    fsClients[0] = ids[3] // Exec
    fsClients[1] = ids[4] // Shell
    consoleClients[0] = ids[4] // Shell
    consoleClients[1] = ids[3] // Exec
    if !serviceEndpoint(ids[0], ids[1], &mut diskReceive, &mut diskSend) ||
        !serviceEndpointMulti(ids[1], &fsClients[0], 2, &mut fsReceive, &mut fsSend[0]) ||
        !serviceEndpointMulti(ids[2], &consoleClients[0], 2, &mut consoleReceive, &mut consoleSend[0]) ||
        !serviceEndpoint(ids[3], ids[4], &mut execReceive, &mut execSend) return shellRollback(&ids[0])
    let diskIrq: UWord = irqGrant(ids[0], serviceDiskIrq(kernelBootInfo.disk))
    let inputIrq: UWord = irqGrant(ids[4], deviceRoleIrq(DEVICE_ROLE_INPUT))
    if diskIrq == 0 || inputIrq == 0 ||
        !diskDevicesInitWritable(ids[0], kernelBootInfo.disk, kernelBootInfo.imageSize) ||
        !inputDevicesInit(ids[4], inputIrq) ||
        !shellServiceStart(ids[0], START_ROLE_DISK, diskReceive, 0, diskIrq) ||
        !shellServiceStart(ids[1], START_ROLE_FILE, fsReceive, diskSend, 0) ||
        !serviceConsoleStart(ids[2], consoleReceive) return shellRollback(&ids[0])
    // Exec: its start endpoint is the receive handle; Fs and the console follow.
    let mut execHandles: UWord[2]
    execHandles[0] = fsSend[0]
    execHandles[1] = consoleSend[1]
    let mut shellHandleList: UWord[3]
    shellHandleList[0] = fsSend[1]
    shellHandleList[1] = execSend
    shellHandleList[2] = consoleSend[0]
    if !serviceHandles(ids[3], &execHandles[0], 2) || !serviceHandles(ids[4], &shellHandleList[0], 3) ||
        !taskInstallRuntimeStart(ids[3], execReceive, RIGHT_RECEIVE, 0) ||
        !taskInstallRuntimeStart(ids[4], 0, 0, 0) return shellRollback(&ids[0])
    let exec: *mut Task = taskGet(ids[3])
    exec.createImages = IMAGE_LOAD_AUTHORITY
    let shell: *mut Task = taskGet(ids[4])
    shell.deviceRights = DEVICE_INPUT
    for i: UWord in 0..SHELL_TASKS {
        if !taskPublish(ids[i]) {
            panic("invalid shell service publication", null)
            return false
        }
    }
    return true
}
export { bootstrapShellInit }

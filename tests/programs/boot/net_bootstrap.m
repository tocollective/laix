// The console server, the Ethernet driver, the IP stack and a client, as separate
// tasks (docs/NETWORK.md).
//
//   client ---- IP ---- driver ---- (kernel broker) ---- card
//      \------- console server
//
// Only the driver holds a device right (the card) and its interrupt token. The IP
// service reaches the wire only through the driver's endpoint; the client reaches
// the network only through the IP service. Handles beyond a start record's one
// endpoint go in the start handle list: driver [interrupt], IP [driver], client
// [IP, console].
import { taskCreateProgram } from "../../../src/task/program.m"
import { taskCreateImage, taskInitAvailable, taskGet, Task, taskPublish, taskDiscardCreated } from "../../../src/task/task.m"
import { taskInstallRuntimeStart } from "../../../src/task/control.m"
import { serviceEndpoint, serviceConsoleStart, serviceHandles } from "service_policy.m"
import { irqGrant } from "../../../src/drivers/irq.m"
import { DEVICE_ROLE_NET, deviceRoleIrq } from "../../../src/drivers/device_table.m"
import { netDevicesInit, netDevicesRollback } from "../../../src/drivers/net_device.m"
import { panic } from "../../../src/kernel/panic.m"
import { RIGHT_RECEIVE, DEVICE_NET } from "../../../src/arch/wrm081632/defs.m"
extern let netdrvImage: UByte
extern let netdrvImageEnd: UByte
extern let ipImage: UByte
extern let ipImageEnd: UByte
extern let netClientImage: UByte
extern let netClientImageEnd: UByte
extern let bootstrapServerStart: UByte
extern let bootstrapServerEnd: UByte

let NET_TASKS: UWord = 4 // console, driver, ip, client

let netRollback(ids: *UWord): Bool {
    for i: UWord in 0..NET_TASKS {
        if ids[i] != 0 && !taskDiscardCreated(ids[i]) panic("could not discard network service", null)
    }
    netDevicesRollback()
    return false
}

let bootstrapNetInit(): Bool {
    if !taskInitAvailable() return false
    let mut ids: UWord[4]
    for i: UWord in 0..NET_TASKS ids[i] = 0
    ids[0] = taskCreateImage(&bootstrapServerStart as UWord, &bootstrapServerEnd as UWord, 0)
    if ids[0] == 0 return false
    ids[1] = taskCreateProgram(&netdrvImage as UWord, &netdrvImageEnd as UWord)
    if ids[1] == 0 return netRollback(&ids[0])
    ids[2] = taskCreateProgram(&ipImage as UWord, &ipImageEnd as UWord)
    if ids[2] == 0 return netRollback(&ids[0])
    ids[3] = taskCreateProgram(&netClientImage as UWord, &netClientImageEnd as UWord)
    if ids[3] == 0 return netRollback(&ids[0])
    let mut consoleReceive: UWord = 0
    let mut consoleSend: UWord = 0
    let mut driverReceive: UWord = 0
    let mut driverSend: UWord = 0
    let mut ipReceive: UWord = 0
    let mut ipSend: UWord = 0
    if !serviceEndpoint(ids[0], ids[3], &mut consoleReceive, &mut consoleSend) ||
        !serviceEndpoint(ids[1], ids[2], &mut driverReceive, &mut driverSend) ||
        !serviceEndpoint(ids[2], ids[3], &mut ipReceive, &mut ipSend) return netRollback(&ids[0])
    let irq: UWord = irqGrant(ids[1], deviceRoleIrq(DEVICE_ROLE_NET))
    if irq == 0 || !netDevicesInit(ids[1], irq) || !serviceConsoleStart(ids[0], consoleReceive) return netRollback(&ids[0])
    let mut driverHandles: UWord[1]
    driverHandles[0] = irq
    let mut ipHandles: UWord[1]
    ipHandles[0] = driverSend
    let mut clientHandles: UWord[2]
    clientHandles[0] = ipSend
    clientHandles[1] = consoleSend
    if !serviceHandles(ids[1], &driverHandles[0], 1) || !serviceHandles(ids[2], &ipHandles[0], 1) ||
        !serviceHandles(ids[3], &clientHandles[0], 2) ||
        !taskInstallRuntimeStart(ids[1], driverReceive, RIGHT_RECEIVE, 0) ||
        !taskInstallRuntimeStart(ids[2], ipReceive, RIGHT_RECEIVE, 0) ||
        !taskInstallRuntimeStart(ids[3], 0, 0, 0) return netRollback(&ids[0])
    let driver: *mut Task = taskGet(ids[1])
    driver.deviceRights = DEVICE_NET
    for i: UWord in 0..NET_TASKS {
        if !taskPublish(ids[i]) {
            panic("invalid network service publication", null)
            return false
        }
    }
    return true
}
export { bootstrapNetInit }

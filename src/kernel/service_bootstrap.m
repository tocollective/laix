// Fixed boot policy for three embedded images. No task/device constructor is
// callable from user mode. Both endpoint roots are attenuated before entry.
import { taskInitAvailable, taskGet, Task, taskDiscardCreated,
    taskInstallServiceStart, taskPublish } from "../task/task.m"
import { taskCreateProgram } from "../task/program.m"
import { ServiceStart } from "../task/service_start.m"
import { serviceEndpoint } from "service_policy.m"
import { mmuGrantResource } from "../mm/mmu.m"
import { irqGrant } from "../drivers/irq.m"
import { serviceDiskIrq, serviceDevicesInit, serviceDevicesRollback } from "../drivers/service_devices.m"
import { kernelBootInfo } from "boot.m"
import { fontData, fontDataEnd } from "../console/font/data.m"
import { panic } from "panic.m"
import { START_MAGIC, SERVICE_START_VERSION, SERVICE_START_BYTES, START_DATA_VA,
    START_ROLE_SERVER, START_ROLE_STORAGE, START_ROLE_CLIENT, START_PROTOCOL_SCREEN,
    START_PROTOCOL_FONT, RIGHT_RECEIVE, RIGHT_SEND, DEVICE_SCREEN, DEVICE_FONT,
    PAGE_SIZE, PAGE_MASK, IPC_MESSAGE_MAX, PTE_U, PTE_RO, PTE_RW, VIDEO_BASE,
    VRAM_BASE, SCREEN_VRAM_VA, SCREEN_VIDEO_VA, SCREEN_FONT_VA,
    SCREEN_VRAM_BYTES, VIDEO_IRQ } from "../arch/wrm081632/defs.m"

extern let screenImage: UByte
extern let screenImageEnd: UByte
extern let storageImage: UByte
extern let storageImageEnd: UByte
extern let applicationImage: UByte
extern let applicationImageEnd: UByte

let serviceRollback(screen: UWord, storage: UWord, client: UWord): Bool {
    if client != 0 && !taskDiscardCreated(client) panic("could not discard screen client", null)
    if screen != 0 && !taskDiscardCreated(screen) panic("could not discard screen server", null)
    if storage != 0 && !taskDiscardCreated(storage) panic("could not discard bitmap server", null)
    serviceDevicesRollback()
    return false
}

let serviceInstall(id: UWord, role: UWord, endpoint: UWord, bitmap: UWord, irq: UWord): Bool {
    let mut block: ServiceStart
    block.magic = START_MAGIC
    block.version = SERVICE_START_VERSION
    block.bytes = SERVICE_START_BYTES
    block.role = role
    block.taskId = id
    block.endpoint = endpoint
    block.rights = RIGHT_RECEIVE
    block.devices = DEVICE_SCREEN
    block.data = START_DATA_VA
    block.dataBytes = PAGE_SIZE
    block.ipcLimit = IPC_MESSAGE_MAX
    block.protocol = START_PROTOCOL_SCREEN
    block.bitmapEndpoint = bitmap
    block.fontIndex = 0
    block.fontBytes = 0
    block.irq = irq
    if role == START_ROLE_SERVER {
        block.fontIndex = SCREEN_FONT_VA
        block.fontBytes = (&fontDataEnd as UWord) - (&fontData as UWord)
    } else if role == START_ROLE_STORAGE {
        block.devices = DEVICE_FONT
        block.protocol = START_PROTOCOL_FONT
    } else {
        block.devices = 0
        block.rights = RIGHT_SEND
    }
    return taskInstallServiceStart(id, &block, serviceDiskIrq(kernelBootInfo.disk))
}

let bootstrapScreenInit(): Bool {
    if !taskInitAvailable() return false
    let screen: UWord = taskCreateProgram(&screenImage as UWord, &screenImageEnd as UWord)
    if screen == 0 return false
    let storage: UWord = taskCreateProgram(&storageImage as UWord, &storageImageEnd as UWord)
    if storage == 0 return serviceRollback(screen, 0, 0)
    let client: UWord = taskCreateProgram(&applicationImage as UWord, &applicationImageEnd as UWord)
    if client == 0 return serviceRollback(screen, storage, 0)
    let mut screenReceive: UWord = 0
    let mut screenSend: UWord = 0
    let mut storageReceive: UWord = 0
    let mut storageSend: UWord = 0
    if !serviceEndpoint(screen, client, &mut screenReceive, &mut screenSend) ||
        !serviceEndpoint(storage, screen, &mut storageReceive, &mut storageSend) ||
        !serviceDevicesInit(screen, storage, kernelBootInfo.disk, kernelBootInfo.imageSize) {
        return serviceRollback(screen, storage, client)
    }
    let screenTask: *mut Task = taskGet(screen)
    let fontStart: UWord = &fontData as UWord
    let fontBytes: UWord = ((&fontDataEnd as UWord) - fontStart + PAGE_MASK) & ~PAGE_MASK
    if !mmuGrantResource(screenTask.directory, screen, SCREEN_VRAM_VA, VRAM_BASE,
        SCREEN_VRAM_BYTES, PTE_RW | PTE_U) ||
        !mmuGrantResource(screenTask.directory, screen, SCREEN_VIDEO_VA, VIDEO_BASE, PAGE_SIZE, PTE_RO | PTE_U) ||
        !mmuGrantResource(screenTask.directory, screen, SCREEN_FONT_VA, fontStart, fontBytes, PTE_RO | PTE_U) {
        return serviceRollback(screen, storage, client)
    }
    let screenIrq: UWord = irqGrant(screen, VIDEO_IRQ)
    let storageIrq: UWord = irqGrant(storage, serviceDiskIrq(kernelBootInfo.disk))
    if screenIrq == 0 || storageIrq == 0 ||
        !serviceInstall(screen, START_ROLE_SERVER, screenReceive, storageSend, screenIrq) ||
        !serviceInstall(storage, START_ROLE_STORAGE, storageReceive, 0, storageIrq) ||
        !serviceInstall(client, START_ROLE_CLIENT, screenSend, 0, 0) return serviceRollback(screen, storage, client)
    if !taskPublish(screen) || !taskPublish(storage) || !taskPublish(client) {
        panic("invalid screen bootstrap publication", null)
        return false
    }
    return true
}

export { bootstrapScreenInit }

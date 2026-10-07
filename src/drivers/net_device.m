// Ethernet card broker (docs/NETWORK.md). The card moves frames by DMA through two
// rings of descriptors that name physical buffers, so neither the rings nor the
// buffers may ever be visible to user code. The kernel allocates them, programs
// the card, and lets the one owner (a user driver) hand over a frame to send and
// take back a received frame, both by copy and both whole. A frame is 14 to 1514
// bytes. The card does DMA only inside a store to TX_KICK or while the host polls
// the network, and only while its CONTROL bit is set, so clearing CONTROL is a
// complete stop; the buffers are released after that.
import { ETH_BASE, PAGE_SIZE, PTE_W, NET_FRAME_MIN, NET_FRAME_MAX, NET_INFO_BYTES,
    ERRNO_EPERM, ERRNO_EFAULT, ERRNO_EBUSY, ERRNO_EINVAL, ERRNO_EIO, ERRNO_EAGAIN } from "../arch/wrm081632/defs.m"
import { PAGE_NONE, PAGE_KERNEL, allocPage, freePage, retainPage, releasePage } from "../mm/memory.m"
import { Task, taskGet, TASK_CREATED, TASK_DEAD } from "../task/task.m"
import { mmuUserBufferValid, copyToUser, copyFromUser } from "../mm/mmu.m"
import { irqTokenValid, irqPollComplete } from "irq.m"
import { DEVICE_ROLE_NET, deviceRoleIrq } from "device_table.m"
import { objectAssertAtomic } from "../ipc/objects.m"
import { panic } from "../kernel/panic.m"

let NET_RX_COUNT: UWord = 16
let NET_TX_COUNT: UWord = 4
let NET_BUFFER_BYTES: UWord = 2048
let NET_PAGE_COUNT: UWord = 11 // one ring page, eight receive pages, two send pages
let NET_TX_RING_OFFSET: UWord = 256
let NET_BUFFER_OWNER: UWord = 0xFFFFFFFC

// Register words (offset / 4) and bits of the card.
let ETH_STATUS: UWord = 0
let ETH_CONTROL: UWord = 1
let ETH_PENDING: UWord = 2
let ETH_MAC_LO: UWord = 3
let ETH_MAC_HI: UWord = 4
let ETH_RX_RING: UWord = 5
let ETH_RX_SIZE: UWord = 6
let ETH_TX_RING: UWord = 8
let ETH_TX_SIZE: UWord = 9
let ETH_TX_KICK: UWord = 11
let ETH_ENABLE_RX_IRQ: UWord = 3 // CONTROL: on, interrupt on RX and LOST
let ETH_PENDING_RX_LOST: UWord = 5
let ETH_PENDING_TX: UWord = 2
let ETH_PENDING_FAULT: UWord = 8
let ETH_PENDING_ALL: UWord = 15
let ETH_DESC_OWN: UWord = 0x80000000
let ETH_DESC_ERROR: UWord = 0x40000000
let ETH_DESC_LENGTH: UWord = 0xFFFF

let mut netOwner: UWord
let mut netToken: UWord
let mut netPages: UWord[11]
let mut netRxNext: UWord
let mut netTxNext: UWord
let mut netFailed: Bool
let mut netDropped: UWord // received frames discarded here: errors, runts, an unreadable destination
let mut netInfoBuffer: UByte[16]

let netRegisters(): *volatile mut UWord {
    return ETH_BASE as *volatile mut UWord
}

// Physical buffer of a receive or send slot: two 2,048-byte buffers to a page.
let netBuffer(index: UWord, send: Bool): UWord {
    let mut first: UWord = 1
    if send first = 1 + NET_RX_COUNT / 2
    return netPages[first + index / 2] + (index % 2) * NET_BUFFER_BYTES
}

let netDescriptor(send: Bool, index: UWord): *volatile mut UWord {
    let mut base: UWord = netPages[0]
    if send base += NET_TX_RING_OFFSET
    return (base + index * 8) as *volatile mut UWord
}

let netFreePages(): Void {
    for i: UWord in 0..NET_PAGE_COUNT {
        if netPages[i] != 0 {
            // Pinned while the card could reach it; a failure here is a kernel bug.
            if !releasePage(netPages[i], NET_BUFFER_OWNER, PAGE_KERNEL) ||
                !freePage(netPages[i], NET_BUFFER_OWNER, PAGE_KERNEL) panic("could not release network DMA page", null)
            netPages[i] = 0
        }
    }
}

// Stops the card and returns every buffer. The registers are idle before any
// page goes back to the allocator.
let netStop(): Void {
    let registers: *volatile mut UWord = netRegisters()
    registers[ETH_CONTROL] = 0
    fence()
    registers[ETH_PENDING] = ETH_PENDING_ALL
    fence()
    netFreePages()
    netOwner = 0
    netToken = 0
    netRxNext = 0
    netTxNext = 0
    netFailed = false
}

// Boot policy: the owner is an unpublished task holding the NET interrupt token.
let netDevicesInit(owner: UWord, token: UWord): Bool {
    objectAssertAtomic()
    let task: *mut Task = taskGet(owner)
    if netOwner != 0 || task == null || task.state != TASK_CREATED ||
        !irqTokenValid(owner, token, deviceRoleIrq(DEVICE_ROLE_NET)) return false
    let registers: *volatile mut UWord = netRegisters()
    registers[ETH_CONTROL] = 0 // the rings can be written only while the card is off
    fence()
    registers[ETH_PENDING] = ETH_PENDING_ALL
    fence()
    for i: UWord in 0..NET_PAGE_COUNT {
        netPages[i] = allocPage(NET_BUFFER_OWNER, PAGE_KERNEL)
        if netPages[i] == PAGE_NONE {
            netPages[i] = 0
            netFreePages()
            return false
        }
        if !retainPage(netPages[i], NET_BUFFER_OWNER, PAGE_KERNEL) {
            if !freePage(netPages[i], NET_BUFFER_OWNER, PAGE_KERNEL) panic("invalid network DMA reservation", null)
            netPages[i] = 0
            netFreePages()
            return false
        }
    }
    let ring: *mut UWord = netPages[0] as *mut UWord
    for i: UWord in 0..(PAGE_SIZE / 4) ring[i] = 0
    for i: UWord in 0..NET_RX_COUNT {
        let descriptor: *volatile mut UWord = netDescriptor(false, i)
        descriptor[0] = netBuffer(i, false)
        descriptor[1] = NET_BUFFER_BYTES | ETH_DESC_OWN
    }
    for i: UWord in 0..NET_TX_COUNT {
        let descriptor: *volatile mut UWord = netDescriptor(true, i)
        descriptor[0] = netBuffer(i, true)
        descriptor[1] = 0
    }
    registers[ETH_RX_RING] = netPages[0]
    registers[ETH_RX_SIZE] = NET_RX_COUNT
    registers[ETH_TX_RING] = netPages[0] + NET_TX_RING_OFFSET
    registers[ETH_TX_SIZE] = NET_TX_COUNT
    fence()
    registers[ETH_CONTROL] = ETH_ENABLE_RX_IRQ
    fence()
    if registers[ETH_PENDING] & ETH_PENDING_FAULT != 0 {
        netStop()
        return false
    }
    netOwner = owner
    netToken = token
    netRxNext = 0
    netTxNext = 0
    netFailed = false
    return true
}

let netCheck(owner: UWord): Word {
    if owner == 0 || owner != netOwner return -ERRNO_EPERM
    let task: *mut Task = taskGet(owner)
    if task == null || task.state == TASK_DEAD || !irqTokenValid(owner, netToken, deviceRoleIrq(DEVICE_ROLE_NET)) return -ERRNO_EPERM
    if netFailed return -ERRNO_EIO
    return 0
}

// 16 bytes: the six MAC bytes, link, a reserved byte, then two words: frames
// the card reported lost, frames this broker discarded.
let netInfo(owner: UWord, destination: UWord): Word {
    objectAssertAtomic()
    let status: Word = netCheck(owner)
    if status != 0 return status
    let task: *mut Task = taskGet(owner)
    if !mmuUserBufferValid(task.directory, owner, destination, NET_INFO_BYTES, PTE_W) return -ERRNO_EFAULT
    let registers: *volatile mut UWord = netRegisters()
    let low: UWord = registers[ETH_MAC_LO]
    let high: UWord = registers[ETH_MAC_HI]
    for i: UWord in 0..NET_INFO_BYTES netInfoBuffer[i] = 0
    for i: UWord in 0..4 netInfoBuffer[i] = ((low >> (8 * i)) & 255) as UByte
    for i: UWord in 0..2 netInfoBuffer[4 + i] = ((high >> (8 * i)) & 255) as UByte
    netInfoBuffer[6] = (registers[ETH_STATUS] & 1) as UByte
    for i: UWord in 0..4 netInfoBuffer[12 + i] = ((netDropped >> (8 * i)) & 255) as UByte
    let result: Word = copyToUser(task.directory, owner, destination, &netInfoBuffer[0], NET_INFO_BYTES)
    if result != 0 return result
    return NET_INFO_BYTES as Word
}

// Sends one frame. The card sends inside the store to TX_KICK, so the slot is
// free again when this returns. Result: the length, or a negative errno.
let netSend(owner: UWord, source: UWord, length: UWord): Word {
    objectAssertAtomic()
    let status: Word = netCheck(owner)
    if status != 0 return status
    if length < NET_FRAME_MIN || length > NET_FRAME_MAX return -ERRNO_EINVAL
    let task: *mut Task = taskGet(owner)
    let descriptor: *volatile mut UWord = netDescriptor(true, netTxNext)
    if descriptor[1] & ETH_DESC_OWN != 0 return -ERRNO_EBUSY
    let copied: Word = copyFromUser(task.directory, owner, netBuffer(netTxNext, true) as *mut UByte, source, length)
    if copied != 0 return copied
    descriptor[1] = length | ETH_DESC_OWN
    fence()
    let registers: *volatile mut UWord = netRegisters()
    registers[ETH_TX_KICK] = 1
    fence()
    let pending: UWord = registers[ETH_PENDING]
    registers[ETH_PENDING] = ETH_PENDING_TX
    fence()
    netTxNext = (netTxNext + 1) % NET_TX_COUNT
    if pending & ETH_PENDING_FAULT != 0 {
        netFailed = true // the card stopped itself at a descriptor it could not use
        return -ERRNO_EIO
    }
    let control: UWord = descriptor[1]
    if control & ETH_DESC_OWN != 0 || control & ETH_DESC_ERROR != 0 {
        netFailed = true
        return -ERRNO_EIO
    }
    return length as Word
}

// Takes the oldest received frame, or returns -EAGAIN with the interrupt armed:
// a frame that arrives afterwards raises the line and wakes the owner's wait.
// capacity must hold the largest frame.
let netRecv(owner: UWord, destination: UWord, capacity: UWord): Word {
    objectAssertAtomic()
    let status: Word = netCheck(owner)
    if status != 0 return status
    if capacity < NET_FRAME_MAX return -ERRNO_EINVAL
    let task: *mut Task = taskGet(owner)
    let registers: *volatile mut UWord = netRegisters()
    let mut attempt: UWord = 0
    while attempt < 3 {
        let descriptor: *volatile mut UWord = netDescriptor(false, netRxNext)
        let control: UWord = descriptor[1]
        if control & ETH_DESC_OWN == 0 {
            let length: UWord = control & ETH_DESC_LENGTH
            let mut result: Word = length as Word
            if control & ETH_DESC_ERROR != 0 || length < NET_FRAME_MIN || length > NET_BUFFER_BYTES {
                netDropped += 1
                result = -ERRNO_EAGAIN // nothing usable here; the caller asks again
            } else {
                let copied: Word = copyToUser(task.directory, owner, destination,
                    netBuffer(netRxNext, false) as *UByte, length)
                if copied != 0 {
                    netDropped += 1
                    result = copied
                }
            }
            // The slot goes back to the card whatever happened to the frame.
            descriptor[1] = NET_BUFFER_BYTES | ETH_DESC_OWN
            fence()
            netRxNext = (netRxNext + 1) % NET_RX_COUNT
            if result != -ERRNO_EAGAIN return result
        } else if registers[ETH_PENDING] & ETH_PENDING_FAULT != 0 {
            netFailed = true
            return -ERRNO_EIO
        } else if attempt == 0 {
            // Empty: acknowledge the card's causes, then look once more before arming.
            registers[ETH_PENDING] = ETH_PENDING_RX_LOST
            fence()
        } else {
            let armed: Word = irqPollComplete(owner, netToken)
            if armed == 0 return -ERRNO_EAGAIN
            if armed != -ERRNO_EBUSY return armed
        }
        attempt += 1
    }
    return -ERRNO_EAGAIN
}

let netReleaseOwner(owner: UWord): Void {
    objectAssertAtomic()
    if owner == 0 || owner != netOwner return
    netStop()
}

// Boot rollback before the owner exists: whatever init built is undone.
let netDevicesRollback(): Void {
    objectAssertAtomic()
    if netOwner == 0 return
    netStop()
}

export { netDevicesInit, netInfo, netSend, netRecv, netReleaseOwner, netDevicesRollback }

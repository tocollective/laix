// Keyboard authority stays in this broker; user services receive events only.
import { KEYBOARD_BASE, PTE_W, ERRNO_EPERM, ERRNO_EFAULT,
    ERRNO_EBUSY, ERRNO_EINVAL } from "../arch/wrm081632/defs.m"
import { Task, taskGet, TASK_CREATED, TASK_DEAD } from "../task/task.m"
import { mmuUserBufferValid, copyToUser } from "../mm/mmu.m"
import { irqTokenValid, irqPollComplete } from "irq.m"
import { DEVICE_ROLE_INPUT, deviceRoleIrq } from "device_table.m"
import { objectAssertAtomic } from "../ipc/objects.m"

let mut inputOwner: UWord
let mut inputToken: UWord
let mut inputSnapshot: UByte[136]

let inputDevicesInit(owner: UWord, token: UWord): Bool {
    objectAssertAtomic()
    let task: *mut Task = taskGet(owner)
    if inputOwner != 0 || task == null || task.state != TASK_CREATED ||
        !irqTokenValid(owner, token, deviceRoleIrq(DEVICE_ROLE_INPUT)) return false
    let registers: *volatile mut UWord = KEYBOARD_BASE as *volatile mut UWord
    registers[2] = 1 // start with an empty event generation
    fence()
    if registers[0] & 1 != 0 return false // also consume the old overflow generation
    inputOwner = owner
    inputToken = token
    return true
}

let inputStoreWord(offset: UWord, word: UWord): Void {
    for byte: UWord in 0..4 inputSnapshot[offset + byte] = ((word >> (byte * 8)) & 255) as UByte
}

// Raw bounded FIFO batch. Queue size, delivery batches, overflow policy and
// interpretation of HID events belong to the user Input service.
let inputRead(owner: UWord, destination: UWord, capacity: UWord): Word {
    objectAssertAtomic()
    if owner == 0 || owner != inputOwner return -ERRNO_EPERM
    let task: *mut Task = taskGet(owner)
    if task == null || task.state == TASK_DEAD ||
        !irqTokenValid(owner, inputToken, deviceRoleIrq(DEVICE_ROLE_INPUT)) return -ERRNO_EPERM
    if capacity == 0 || capacity > 32 return -ERRNO_EINVAL
    let bytes: UWord = 8 + capacity * 4
    if !mmuUserBufferValid(task.directory, owner, destination, bytes, PTE_W) return -ERRNO_EFAULT
    let registers: *volatile mut UWord = KEYBOARD_BASE as *volatile mut UWord
    let mut count: UWord = 0
    let mut overflow: Bool = false
    for i: UWord in 0..bytes inputSnapshot[i] = 0
    for i: UWord in 0..capacity {
        let status: UWord = registers[0]
        if status & 2 != 0 overflow = true
        if status & 1 == 0 break
        inputStoreWord(8 + count * 4, registers[1])
        count += 1
    }
    fence()
    let armed: Word = irqPollComplete(owner, inputToken)
    if armed != 0 && armed != -ERRNO_EBUSY return armed
    inputStoreWord(0, count)
    if overflow inputStoreWord(4, 1)
    let result: Word = copyToUser(task.directory, owner, destination, &inputSnapshot[0], bytes)
    if result != 0 return result
    return bytes as Word
}

let inputReleaseOwner(owner: UWord): Void {
    objectAssertAtomic()
    if owner == 0 || owner != inputOwner return
    let registers: *volatile mut UWord = KEYBOARD_BASE as *volatile mut UWord
    registers[2] = 1
    fence()
    inputOwner = 0
    inputToken = 0
}

export { inputDevicesInit, inputRead, inputReleaseOwner }

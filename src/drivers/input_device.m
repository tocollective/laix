// Keyboard authority stays in this broker; user services receive events only.
import { KEYBOARD_BASE, KEYBOARD_IRQ, PTE_W, ERRNO_EPERM, ERRNO_EFAULT,
    ERRNO_EBUSY } from "../arch/wrm081632/defs.m"
import { Task, taskGet, TASK_CREATED } from "../task/task.m"
import { mmuUserBufferValid, copyToUser } from "../mm/mmu.m"
import { irqTokenValid, irqPollComplete } from "irq.m"
import { objectAssertAtomic } from "../ipc/objects.m"

let mut inputOwner: UWord
let mut inputToken: UWord
let mut inputEvents: UWord[32]
let mut inputHead: UWord
let mut inputCount: UWord
let mut inputOverflow: Bool
let mut inputSnapshot: UByte[24]

let inputDevicesInit(owner: UWord, token: UWord): Bool {
    objectAssertAtomic()
    let task: *mut Task = taskGet(owner)
    if inputOwner != 0 || task == null || task.state != TASK_CREATED ||
        !irqTokenValid(owner, token, KEYBOARD_IRQ) return false
    let registers: *volatile mut UWord = KEYBOARD_BASE as *volatile mut UWord
    registers[2] = 1 // start with an empty event generation
    fence()
    inputOverflow = registers[0] & 2 != 0 // read-to-clear overflow
    inputOwner = owner
    inputToken = token
    inputHead = 0
    inputCount = 0
    inputOverflow = false
    return true
}

let inputStoreWord(offset: UWord, word: UWord): Void {
    for byte: UWord in 0..4 inputSnapshot[offset + byte] = ((word >> (byte * 8)) & 255) as UByte
}

// Drain at most one hardware FIFO's capacity. All accesses are bounded even
// if new host events arrive while servicing; a remaining level stays masked.
let inputRead(owner: UWord, destination: UWord): Word {
    objectAssertAtomic()
    if owner == 0 || owner != inputOwner return -ERRNO_EPERM
    let task: *mut Task = taskGet(owner)
    if !mmuUserBufferValid(task.directory, owner, destination, 24, PTE_W) return -ERRNO_EFAULT
    let registers: *volatile mut UWord = KEYBOARD_BASE as *volatile mut UWord
    for i: UWord in 0..32 {
        let status: UWord = registers[0]
        if status & 2 != 0 inputOverflow = true
        if status & 1 == 0 break
        let event: UWord = registers[1]
        if inputCount == 32 inputOverflow = true
        else {
            inputEvents[(inputHead + inputCount) % 32] = event
            inputCount += 1
        }
    }
    fence()
    let armed: Word = irqPollComplete(owner, inputToken)
    if armed != 0 && armed != -ERRNO_EBUSY return armed
    let mut count: UWord = inputCount
    if count > 4 count = 4
    for i: UWord in 0..24 inputSnapshot[i] = 0
    inputStoreWord(0, count)
    if inputOverflow inputStoreWord(4, 1)
    for i: UWord in 0..count inputStoreWord(8 + i * 4, inputEvents[(inputHead + i) % 32])
    let result: Word = copyToUser(task.directory, owner, destination, &inputSnapshot[0], 24)
    if result != 0 return result
    inputHead = (inputHead + count) % 32
    inputCount -= count
    inputOverflow = false
    return 24
}

let inputReleaseOwner(owner: UWord): Void {
    objectAssertAtomic()
    if owner == 0 || owner != inputOwner return
    let registers: *volatile mut UWord = KEYBOARD_BASE as *volatile mut UWord
    registers[2] = 1
    fence()
    inputOwner = 0
    inputToken = 0
    inputCount = 0
    inputHead = 0
    inputOverflow = false
    for i: UWord in 0..32 inputEvents[i] = 0
}

export { inputDevicesInit, inputRead, inputReleaseOwner }

// Two death orders run with the same image and different startup arguments.
import { RuntimeStart, TaskEvent } from "../../../src/task/runtime_start.m"
import { openMemorySpace, allocateRegion, mapRegion, grantRegion, mapGrantedRegion,
    inspectTask, collectTask, createTask, configureTask, publishTask, closeMemoryGrant,
    send, recv, yield, exit } from "../../../user/syscalls.m"
import { PAGE_SIZE, PTE_RW, PTE_U, MEM_VA_START, TASK_EVENT_RECLAIMED,
    ERRNO_EAGAIN, RUNTIME_START_BYTES } from "../../../src/arch/wrm081632/defs.m"
let mut word: UWord
let mut event: TaskEvent
extern let sharingOwnerGone(): Void
extern let sharingBorrowerGone(): Void
extern let sharingBothGone(): Void

let sharingExpect(value: Bool, code: Word): Void { if !value exit(code) }
let sharingSend(handle: UWord, value: UWord): Void {
    word = value
    sharingExpect(send(handle, (&word) as *UByte, 4) == 4, 2)
}
let sharingReceive(handle: UWord): UWord {
    sharingExpect(recv(handle, (&mut word) as *mut UByte, 4) == 4, 3)
    return word
}
let sharingWaitReap(reference: UWord): Void {
    while true {
        sharingExpect(inspectTask(reference, &mut event) == 0, 4)
        if event.flags & TASK_EVENT_RECLAIMED != 0 break
        sharingExpect(yield() == 0, 5)
    }
    sharingExpect(event.code == 0, 6)
}

let sharingUserMain(start: *RuntimeStart, bytes: UWord): Void {
    sharingExpect(bytes == RUNTIME_START_BYTES, 1)
    let data: *UWord = start.data as *UWord
    let role: UWord = start.argument % 10
    let order: UWord = start.argument / 10 // 0 owner first; 1 borrower first
    if role == 0 {
        let space: Word = openMemorySpace(0, 47)
        sharingExpect(space > 0, 7)
        let region: Word = allocateRegion(space as UWord, 2)
        sharingExpect(region > 0, 8)
        sharingExpect(mapRegion(space as UWord, region as UWord, MEM_VA_START, 0, 2, PTE_RW | PTE_U) == 0, 9)
        let shared: *mut UWord = MEM_VA_START as *mut UWord
        shared[0] = 0x12345678
        shared[PAGE_SIZE / 4] = 0x87654321
        let revoked: Word = grantRegion(space as UWord, region as UWord, 2, PTE_RW | PTE_U)
        sharingExpect(revoked > 0 && closeMemoryGrant(revoked as UWord) == 0, 26)
        let grant: Word = grantRegion(space as UWord, region as UWord, 2, PTE_RW | PTE_U)
        sharingExpect(grant > 0, 10)
        sharingSend(start.endpoint, grant as UWord)
        sharingExpect(sharingReceive(start.endpoint) == 1, 11)
        sharingSend(data[0], 1)
        sharingExpect(sharingReceive(data[1]) == 1, 12)
        // Borrower-first teardown must retain the owner's allocation and data.
        sharingExpect(shared[0] == 0x12345678 && shared[PAGE_SIZE / 4] == 0x87654321, 13)
        exit(0)
    }
    if role == 1 {
        let space: Word = openMemorySpace(0, 15)
        sharingExpect(space > 0, 14)
        let grant: UWord = sharingReceive(start.endpoint)
        sharingExpect(mapGrantedRegion(space as UWord, grant, MEM_VA_START, PTE_RW | PTE_U) == 0, 15)
        let shared: *mut UWord = MEM_VA_START as *mut UWord
        sharingExpect(shared[0] == 0x12345678 && shared[PAGE_SIZE / 4] == 0x87654321, 16)
        sharingSend(start.endpoint, 1)
        sharingExpect(sharingReceive(data[1]) == 1, 17)
        // In owner-first mode this is after owner reaping and slot reuse.
        sharingExpect(shared[0] == 0x12345678 && shared[PAGE_SIZE / 4] == 0x87654321, 18)
        sharingSend(data[0], 2)
        exit(0)
    }
    sharingExpect(role == 2 && sharingReceive(start.endpoint) == 1, 19)
    let mut replacement: UWord = 0
    if order == 0 {
        sharingSend(data[0], 1)
        sharingWaitReap(1)
        let child: Word = createTask(1)
        sharingExpect(child == 257, 20)
        replacement = child as UWord
        // Leave replacement Created so its budget is live at the probe marker.
        sharingOwnerGone()
        sharingSend(data[1], 1)
        sharingExpect(sharingReceive(start.endpoint) == 2, 21)
        sharingWaitReap(2)
    } else {
        sharingSend(data[1], 1)
        sharingExpect(sharingReceive(start.endpoint) == 2, 22)
        sharingWaitReap(2)
        sharingBorrowerGone()
        sharingSend(data[0], 1)
        sharingWaitReap(1)
    }
    sharingBothGone()
    if replacement != 0 {
        sharingExpect(configureTask(replacement, 0, 0, 0) == 0 && publishTask(replacement) == 0, 23)
        while true {
            let result: Word = collectTask(replacement, &mut event)
            if result == 0 break
            sharingExpect(result == -ERRNO_EAGAIN && yield() == 0, 24)
        }
        sharingExpect(event.code == 0, 25)
    }
    exit(0)
}
export { sharingUserMain }

// CPU acceptance user image: page-backed heap, real trap/MMU paths and IPC.
import { RuntimeStart, TaskEvent } from "../../../src/task/runtime_start.m"
import { heapAllocate, heapRelease } from "../../../user/heap.m"
import { openMemorySpace, allocateRegion, releaseRegion, mapRegion, unmapRegion,
    protectRegion, populateRegion, closeMemorySpace, createTask, configureTask,
    publishTask, collectTask, send, recv, yield, exit } from "../../../user/syscalls.m"
import { PAGE_SIZE, PTE_RW, PTE_RO, PTE_RX, PTE_U, MEM_VA_START, MEM_RIGHT_ALL,
    ERRNO_EBUSY, ERRNO_EINVAL, ERRNO_EPERM, ERRNO_EAGAIN, ERRNO_ENOMEM,
    RUNTIME_START_MAGIC, RUNTIME_START_VERSION, RUNTIME_START_BYTES } from "../../../src/arch/wrm081632/defs.m"
let mut message: UWord
let mut event: TaskEvent
extern let memoryPayloadStart: UByte
extern let memoryPayloadEnd: UByte
extern let memoryExecute(address: UWord): UWord

let expect(value: Bool, code: Word): Void { if !value exit(code) }
let memoryUserMain(start: *RuntimeStart, bytes: UWord): Void {
    expect(bytes == RUNTIME_START_BYTES && start.magic == RUNTIME_START_MAGIC &&
        start.version == RUNTIME_START_VERSION, 1)
    if start.argument == 1 {
        for i: UWord in 0..20 {
            expect(recv(start.endpoint, (&mut message) as *mut UByte, 4) == 4, 2)
            expect(message == i, 3)
            expect(send(start.endpoint, (&message) as *UByte, 4) == 4, 4)
        }
        exit(0)
    }
    for i: UWord in 0..20 {
        let buffer: *mut UByte = heapAllocate(PAGE_SIZE + 1)
        expect(buffer != null, 5)
        expect(buffer[0] == 0 && buffer[PAGE_SIZE] == 0, 6)
        buffer[0] = 0xA5
        buffer[PAGE_SIZE] = 0x5A
        message = i
        expect(send(start.endpoint, (&message) as *UByte, 4) == 4, 7)
        expect(recv(start.endpoint, (&mut message) as *mut UByte, 4) == 4 && message == i, 8)
        // Enough user instructions for natural timer preemption on this image.
        let mut busy: UWord = 0
        while busy < 5000 busy += 1
        expect(buffer[0] == 0xA5 && buffer[PAGE_SIZE] == 0x5A, 9)
        expect(heapRelease(buffer) == 0, 10)
    }
    let cap: Word = openMemorySpace(0, 15)
    expect(cap > 0, 11)
    let region: Word = allocateRegion(cap as UWord, 2)
    expect(region > 0, 12)
    let virtual: UWord = MEM_VA_START + 0x3FF000
    expect(mapRegion(cap as UWord, region as UWord, virtual, 0, 2, PTE_RW | PTE_U) == 0, 13)
    expect(releaseRegion(cap as UWord, region as UWord) == -ERRNO_EBUSY, 14)
    expect(mapRegion(cap as UWord, region as UWord, virtual - PAGE_SIZE, 0, 2, PTE_RW | PTE_U) == -ERRNO_EBUSY, 15)
    expect(mapRegion(cap as UWord, region as UWord, 0xFFFFF000, 0, 2, PTE_RW | PTE_U) == -ERRNO_EINVAL, 16)
    let data: *mut UByte = virtual as *mut UByte
    let payload: *UByte = &memoryPayloadStart
    let payloadBytes: UWord = (&memoryPayloadEnd as UWord) - (&memoryPayloadStart as UWord)
    for i: UWord in 0..payloadBytes data[i] = payload[i]
    expect(protectRegion(cap as UWord, virtual, 2, PTE_RO | PTE_U) == 0, 17)
    expect(data[0] == payload[0], 18)
    expect(protectRegion(cap as UWord, virtual, 2, PTE_RX | PTE_U) == 0, 19)
    expect(memoryExecute(virtual) == 42, 20)
    expect(mapRegion(cap as UWord, region as UWord, MEM_VA_START, 0, 2, PTE_RW | PTE_U) == -ERRNO_EBUSY, 21)
    expect(mapRegion(cap as UWord, region as UWord, MEM_VA_START, 0, 2, PTE_RX | PTE_U) == 0, 22)
    expect(protectRegion(cap as UWord, virtual, 2, PTE_RW | PTE_U) == -ERRNO_EBUSY, 23)
    expect(unmapRegion(cap as UWord, MEM_VA_START, 2) == 0, 24)
    expect(protectRegion(cap as UWord, virtual, 2, PTE_RW | PTE_U) == 0, 25)
    data[0] = 0
    expect(unmapRegion(cap as UWord, virtual, 2) == 0, 26)
    expect(releaseRegion(cap as UWord, region as UWord) == 0, 27)
    // Exhaust this caller's bounded allocation path, then prove recovery.
    let mut held: UWord[8]
    let mut issued: UWord = 0
    while issued < 8 {
        let next: Word = allocateRegion(cap as UWord, 16)
        if next < 0 {
            expect(next == -ERRNO_ENOMEM, 42)
            break
        }
        held[issued] = next as UWord
        issued += 1
    }
    expect(issued < 8, 43)
    for i: UWord in 0..issued expect(releaseRegion(cap as UWord, held[i]) == 0, 44)
    let recovered: *mut UByte = heapAllocate(PAGE_SIZE)
    expect(recovered != null && recovered[0] == 0, 45)
    expect(heapRelease(recovered) == 0, 46)
    expect(closeMemorySpace(cap as UWord) == 0, 28)
    expect(allocateRegion(cap as UWord, 1) == -ERRNO_EPERM, 29)
    // Unpublished child loading with an expiring, bounded memory capability.
    let child: Word = createTask(1)
    expect(child > 0, 30)
    let loader: Word = openMemorySpace(child as UWord, MEM_RIGHT_ALL)
    expect(loader > 0, 31)
    let code: Word = allocateRegion(loader as UWord, 1)
    expect(code > 0, 32)
    expect(populateRegion(loader as UWord, code as UWord, 0, payload, payloadBytes) == 0, 33)
    expect(mapRegion(loader as UWord, code as UWord, MEM_VA_START, 0, 1, PTE_RW | PTE_U) == 0, 34)
    expect(protectRegion(loader as UWord, MEM_VA_START, 1, PTE_RX | PTE_U) == 0, 35)
    expect(populateRegion(loader as UWord, code as UWord, 0, payload, payloadBytes) == -ERRNO_EBUSY, 36)
    expect(configureTask(child as UWord, 0, 0, 0) == 0, 37)
    expect(publishTask(child as UWord) == 0, 38)
    expect(allocateRegion(loader as UWord, 1) == -ERRNO_EPERM, 39)
    while true {
        let result: Word = collectTask(child as UWord, &mut event)
        if result == 0 break
        expect(result == -ERRNO_EAGAIN, 40)
        yield()
    }
    expect(event.code == 0, 41)
    exit(0)
}
export { memoryUserMain }

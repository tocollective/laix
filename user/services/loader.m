// Files-backed loader acceptance (G1). The only image the kernel was given at
// build time is this one; the child comes from the storage volume through Disk
// and Files, is loaded with SYS_TASK_LOAD, runs, and is collected. Every check
// exits with its own code so a failure names its step.
import { ServiceStart, serviceStartValid } from "../../src/task/service_start.m"
import { TaskEvent } from "../../src/task/runtime_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_CLIENT, START_PROTOCOL_FILE,
    TASK_LOAD_BYTES, TASK_EVENT_FAULT, ERRNO_EINVAL, ERRNO_EFAULT, ERRNO_ENFILE,
    ERRNO_EAGAIN } from "../../src/arch/wrm081632/defs.m"
import { heapAllocate } from "../heap.m"
import { readImage } from "../loadfile.m"
import { loadTask, configureTask, publishTask, collectTask, yield, exit } from "../syscalls.m"
let mut loaderEvent: TaskEvent
let mut loaderImage: Word

// Collect once the child has finished; the final event is copied to loaderEvent.
let loaderWait(reference: UWord): Word {
    while true {
        let result: Word = collectTask(reference, &mut loaderEvent)
        if result == 0 return 0
        if result != -ERRNO_EAGAIN return result
        yield()
    }
}

let loaderStart(reference: UWord, argument: UWord): Word {
    let configured: Word = configureTask(reference, 0, 0, argument)
    if configured != 0 return configured
    return publishTask(reference)
}

// Loads the image in buffer and runs it to completion; returns the exit code,
// or a negative errno from any step.
let loaderRun(buffer: *mut UByte, argument: UWord): Word {
    let reference: Word = loadTask(buffer, loaderImage as UWord)
    if reference < 0 return reference
    let started: Word = loaderStart(reference as UWord, argument)
    if started != 0 return started
    let waited: Word = loaderWait(reference as UWord)
    if waited != 0 return waited
    if loaderEvent.flags & TASK_EVENT_FAULT != 0 return -1000
    return loaderEvent.code
}

let loaderMain(start: *ServiceStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != SERVICE_START_BYTES ||
        !serviceStartValid(start) || start.role != START_ROLE_CLIENT || start.protocol != START_PROTOCOL_FILE) exit(1)
    let files: UWord = start.endpoint
    let buffer: *mut UByte = heapAllocate(TASK_LOAD_BYTES)
    if buffer == null exit(2)

    // 1. The whole volume file crosses Disk and Files in 16-byte pieces.
    loaderImage = readImage(files, buffer, TASK_LOAD_BYTES)
    if loaderImage < 52 exit(3)

    // 2. Four children from those bytes, each with its own argument.
    for argument: UWord in 0..4 {
        let code: Word = loaderRun(buffer, argument)
        if code != ((5 + argument) * 9) as Word exit(10 + argument as Word)
    }

    // 3. A faulting child is reported as a fault, not as an exit code.
    let faulted: Word = loaderRun(buffer, 0xDEAD)
    if faulted != -1000 || loaderEvent.cause != 3 exit(20)

    // 4. Loaded children spend the creator's quota like catalog children: four
    // uncollected completions fill it, whatever slots are free, and the fifth
    // load fails with ENFILE. Collecting frees the rows and loading works again.
    let mut references: UWord[4]
    for i: UWord in 0..4 {
        let reference: Word = loadTask(buffer, loaderImage as UWord)
        if reference < 0 exit(30)
        references[i] = reference as UWord
        if loaderStart(references[i], i) != 0 exit(31)
    }
    if loadTask(buffer, loaderImage as UWord) != -ERRNO_ENFILE exit(32)
    for i: UWord in 0..4 {
        if loaderWait(references[i]) != 0 || loaderEvent.code != ((5 + i) * 9) as Word exit(33)
    }
    if loaderRun(buffer, 3) != 72 exit(34)

    // 5. Bad requests fail without effect: size, buffer, header, segment.
    if loadTask(buffer, TASK_LOAD_BYTES + 1) != -ERRNO_EINVAL exit(40)
    if loadTask(buffer, 16) != -ERRNO_EINVAL exit(41)
    if loadTask(0x61000000 as *UByte, 4096) != -ERRNO_EFAULT exit(42)
    let words: *mut UWord = buffer as *mut UWord
    let magic: UWord = words[0]
    words[0] = magic ^ 1
    if loadTask(buffer, loaderImage as UWord) != -ERRNO_EINVAL exit(43)
    words[0] = magic
    let vaddr: UWord = words[(52 + 8) / 4]
    words[(52 + 8) / 4] = 0
    if loadTask(buffer, loaderImage as UWord) != -ERRNO_EINVAL exit(44)
    words[(52 + 8) / 4] = vaddr

    // 6. Fresh bytes from Files after the failures: nothing was leaked or left
    // half-built, so the same child still loads and runs.
    loaderImage = readImage(files, buffer, TASK_LOAD_BYTES)
    if loaderImage < 52 exit(50)
    if loaderRun(buffer, 3) != 72 exit(51)
    exit(0)
}
export { loaderMain }

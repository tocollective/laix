// Exec: runs a program stored on the filesystem. A request names a file; Exec
// reads it from the filesystem service, asks the kernel to load it as an
// unpublished child (SYS_TASK_LOAD, which checks the image and charges Exec's
// quota), configures it with one endpoint and a numeric argument, publishes it,
// waits for it to finish and reports how. The child's authority is exactly that
// endpoint: Exec decides it, the caller cannot widen it.
//
// Handles (docs/SHELL.md): the start endpoint is Exec's receive handle; the start
// handle list holds [0] the filesystem and [1] the console, as send handles.
import { RuntimeStart, TaskEvent } from "../../src/task/runtime_start.m"
import { START_BLOCK_VA, RUNTIME_START_BYTES, RUNTIME_START_MAGIC, RUNTIME_START_VERSION, TASK_LOAD_BYTES,
    TASK_EVENT_FAULT, EXEC_RUN_HEADER, EXEC_RESPONSE_HEADER, EXEC_FAULTED, DATA_GENERATION,
    RIGHT_SEND, ERRNO_EINVAL, ERRNO_EPIPE, ERRNO_EAGAIN, ERRNO_ETIMEDOUT } from "../../src/arch/wrm081632/defs.m"
import { AcceptResult, accept, reply, loadTask, configureTask, publishTask, collectTask,
    terminateTask, yield, sleep, exit, discard } from "../syscalls.m"
import { heapAllocate } from "../heap.m"
import { startHandle } from "../starthandles.m"
import { wordsGet } from "../words.m"
import { FsFile, fsOpen, fsReadAll } from "fsclient.m"

let EXEC_QUICK_POLLS: UWord = 2000 // yields before Exec starts sleeping between polls

let mut execRequest: UWord[8]
let mut execResponse: UWord[8]
let mut execName: UByte[17]
let mut execEvent: TaskEvent
let mut execFile: FsFile
let mut execImage: *mut UByte
let mut execFs: UWord
let mut execConsole: UWord
let mut execGeneration: UWord = DATA_GENERATION

// Waits for the child. limit is a number of seconds (zero: no limit); after it,
// the child is terminated and the reply is -ETIMEDOUT. Returns zero or an errno.
let execWait(reference: UWord, limit: UWord): Word {
    let mut quick: UWord = 0
    let mut seconds: UWord = 0
    let mut timedOut: Bool = false
    while true {
        let collected: Word = collectTask(reference, &mut execEvent)
        if collected == 0 {
            if timedOut return -ERRNO_ETIMEDOUT
            return 0
        }
        if collected != -ERRNO_EAGAIN return collected
        if limit != 0 && seconds >= limit && !timedOut {
            timedOut = true
            discard(terminateTask(reference, -1))
        }
        if quick < EXEC_QUICK_POLLS || timedOut {
            quick += 1
            yield()
        } else {
            discard(sleep(1))
            seconds += 1
        }
    }
}

// Ends a child that never ran: terminate and collect it so its quota row is free.
let execAbandon(reference: UWord): Void {
    discard(terminateTask(reference, -1))
    for i: UWord in 0..8 {
        if collectTask(reference, &mut execEvent) != -ERRNO_EAGAIN return
        yield()
    }
}

let execHandle(request: *UWord, size: UWord, response: *mut UWord): Void {
    for i: UWord in 0..8 response[i] = 0
    response[0] = EXEC_RESPONSE_HEADER
    response[1] = (-ERRNO_EINVAL) as UWord
    response[2] = execGeneration
    if size != 32 || request[0] != EXEC_RUN_HEADER return
    if request[1] != execGeneration {
        response[1] = (-ERRNO_EPIPE) as UWord
        return
    }
    for i: UWord in 0..16 execName[i] = wordsGet(&request[4], i) as UByte
    execName[16] = 0
    let argument: UWord = request[2]
    let limit: UWord = request[3]
    let opened: Word = fsOpen(execFs, &execName[0], 0, 0, &mut execFile)
    if opened != 0 {
        response[1] = opened as UWord
        return
    }
    let bytes: Word = fsReadAll(execFs, execFile.id, execImage, TASK_LOAD_BYTES)
    if bytes <= 0 {
        // An empty file is not an image; the kernel would refuse it too.
        if bytes == 0 response[1] = (-ERRNO_EINVAL) as UWord
        else response[1] = bytes as UWord
        return
    }
    let reference: Word = loadTask(execImage, bytes as UWord)
    if reference < 0 {
        response[1] = reference as UWord
        return
    }
    let configured: Word = configureTask(reference as UWord, execConsole, RIGHT_SEND, argument)
    if configured != 0 {
        response[1] = configured as UWord
        execAbandon(reference as UWord)
        return
    }
    let published: Word = publishTask(reference as UWord)
    if published != 0 {
        response[1] = published as UWord
        execAbandon(reference as UWord)
        return
    }
    let waited: Word = execWait(reference as UWord, limit)
    response[1] = waited as UWord
    if waited == 0 {
        response[3] = execEvent.code as UWord
        if execEvent.flags & TASK_EVENT_FAULT != 0 {
            response[4] = EXEC_FAULTED
            response[5] = execEvent.cause
        }
    }
}

let execMain(start: *RuntimeStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != RUNTIME_START_BYTES ||
        start.magic != RUNTIME_START_MAGIC || start.version != RUNTIME_START_VERSION) exit(1)
    execFs = startHandle(start, 0)
    execConsole = startHandle(start, 1)
    if start.endpoint == 0 || execFs == 0 || execConsole == 0 exit(2)
    execImage = heapAllocate(TASK_LOAD_BYTES)
    if execImage == null exit(3)
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(start.endpoint, &mut execRequest[0] as *mut UByte, 32, &mut accepted)
        if size < 0 exit(4)
        execHandle(&execRequest[0], size as UWord, &mut execResponse[0])
        let sent: Word = reply(accepted.replyToken, &execResponse[0] as *UByte, 32)
        if sent > 32 exit(5)
    }
}
export { execMain, execHandle }

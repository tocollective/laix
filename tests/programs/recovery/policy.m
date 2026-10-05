// Acceptance user image: real service faults, explicit reconnection and a peer.
import { RuntimeStart, TaskEvent } from "../../../src/task/runtime_start.m"
import { ServiceResolution } from "../../../src/task/recovery_start.m"
import { ManagedService, launchService, retireService, recoveryBackoff } from "../../../user/recovery/policy.m"
import { createTask, configureTask, publishTask, terminateTask, inspectTask,
    collectTask, allowServices, createEndpoint, closeHandle, resolveService,
    callTimed, send, recv, tryRecv, sleep, yield, exit } from "../../../user/syscalls.m"
import { ENDPOINT_MODE_RAW, RIGHT_SEND, RIGHT_RECEIVE, DEVICE_DISK,
    FILE_REQUEST_HEADER, FILE_RESPONSE_HEADER, FILE_FONT_ID, ERRNO_EPIPE,
    ERRNO_EAGAIN, TASK_EVENT_RECLAIMED } from "../../../src/arch/wrm081632/defs.m"
extern let recoveryDone(): Void
let mut message: UWord[8]
let mut answer: UWord[8]
let mut resolution: ServiceResolution
let mut event: TaskEvent
let mut echo: ManagedService
let mut disk: ManagedService
let mut files: ManagedService
let expect(ok: Bool, code: Word): Void { if !ok exit(code) }
let fileRead(handle: UWord, generation: UWord, offset: UWord): Word {
    message[0] = FILE_REQUEST_HEADER
    message[1] = generation
    message[2] = FILE_FONT_ID
    message[3] = offset
    message[4] = 16
    let size: Word = callTimed(handle, &message[0] as *UByte, 20, &mut answer[0] as *mut UByte, 32, 5)
    if size < 0 return size
    expect(size == 32 && answer[0] == FILE_RESPONSE_HEADER && answer[2] != 0, 11)
    return answer[1] as Word
}
let clientMain(start: *RuntimeStart): Void {
    let mut oldEcho: UWord = 0
    let mut oldFiles: UWord = 0
    let mut previousInstance: UWord = 0
    for generation: UWord in 1..6 {
        expect(recv(start.endpoint, &mut message[0] as *mut UByte, 4) == 4 && message[0] == generation, 12)
        expect(resolveService(1, &mut resolution) == 0 && resolution.generation == generation, 13)
        let freshEcho: UWord = resolution.handle
        expect(resolution.instance != previousInstance, 14)
        previousInstance = resolution.instance
        expect(resolveService(2, &mut resolution) == 0 && resolution.generation == generation, 15)
        let freshFiles: UWord = resolution.handle
        // Keep stale handles until fresh ones are installed, then explicitly close.
        if oldEcho != 0 {
            message[0] = 7
            expect(callTimed(oldEcho, &message[0] as *UByte, 4, &mut answer[0] as *mut UByte, 32, 5) == -ERRNO_EPIPE, 16)
            expect(fileRead(oldFiles, generation - 1, 0) == -ERRNO_EPIPE, 17)
            expect(closeHandle(oldEcho) == 0 && closeHandle(oldFiles) == 0, 18)
        }
        message[0] = generation
        expect(callTimed(freshEcho, &message[0] as *UByte, 4, &mut answer[0] as *mut UByte, 32, 5) == 4 && answer[0] == generation, 19)
        expect(fileRead(freshFiles, generation, 0) == 0 && answer[3] == 16 && answer[2] == generation, 20)
        // An old resource incarnation is rejected on the new, live endpoint.
        if generation > 1 expect(fileRead(freshFiles, generation - 1, 0) == -ERRNO_EPIPE, 21)
        if generation < 5 {
            message[0] = 0xDEAD
            expect(callTimed(freshEcho, &message[0] as *UByte, 4, &mut answer[0] as *mut UByte, 32, 5) == -ERRNO_EPIPE, 22)
            expect(fileRead(freshFiles, generation, 32) == -ERRNO_EPIPE, 23)
        }
        oldEcho = freshEcho
        oldFiles = freshFiles
        message[0] = generation
        expect(send(start.endpoint, &message[0] as *UByte, 4) == 4, 24)
    }
    expect(closeHandle(oldEcho) == 0 && closeHandle(oldFiles) == 0, 25)
    exit(0)
}
let waitClient(control: UWord, client: UWord, generation: UWord): Void {
    for wait: UWord in 0..10 {
        let size: Word = tryRecv(control, &mut message[0] as *mut UByte, 4)
        if size == 4 {
            expect(message[0] == generation, 26)
            return
        }
        expect(size == -ERRNO_EAGAIN, 27)
        expect(inspectTask(client, &mut event) == 0 && event.state != 3, 28)
        expect(sleep(1) == 0, 29)
    }
    exit(30)
}
let collect(reference: UWord): Void {
    for wait: UWord in 0..5 {
        expect(inspectTask(reference, &mut event) == 0, 31)
        if event.flags & TASK_EVENT_RECLAIMED != 0 {
            expect(collectTask(reference, &mut event) == 0 && event.code == 0, 32)
            return
        }
        expect(sleep(1) == 0, 33)
    }
    exit(34)
}
let recoveryPolicyMain(start: *RuntimeStart, bytes: UWord): Void {
    if start.argument == 1 clientMain(start)
    if start.argument == 2 {
        // Unrelated CPU work survives every failure, with natural preemption.
        let mut progress: UWord = 0
        while true {
            for i: UWord in 0..10000 progress += 1
            expect(yield() == 0, 35)
        }
    }
    let control: Word = createEndpoint(ENDPOINT_MODE_RAW, 0)
    let client: Word = createTask(5)
    expect(control > 0 && client > 0 && allowServices(client as UWord, 3) == 0, 36)
    expect(configureTask(client as UWord, control as UWord, RIGHT_SEND | RIGHT_RECEIVE, 1) == 0 && publishTask(client as UWord) == 0, 37)
    let peer: Word = createTask(5)
    expect(peer > 0 && configureTask(peer as UWord, 0, 0, 2) == 0 && publishTask(peer as UWord) == 0, 38)
    for generation: UWord in 1..6 {
        expect(launchService(&mut echo, 2, 1, 0, generation, 0) == 0, 39)
        expect(launchService(&mut disk, 3, 3, 0, generation, DEVICE_DISK) == 0, 40)
        expect(launchService(&mut files, 4, 2, disk.root, generation, 0) == 0, 41)
        message[0] = generation
        expect(send(control as UWord, &message[0] as *UByte, 4) == 4, 42)
        waitClient(control as UWord, client as UWord, generation)
        // Consumers retire before the failed producer; replacements reverse order.
        expect(retireService(&mut files, 2) == 0, 43)
        expect(retireService(&mut disk, 3) == 0, 44)
        expect(retireService(&mut echo, 1) == 0, 45)
        expect(inspectTask(peer as UWord, &mut event) == 0 && event.state != 3, 46)
        if generation < 5 expect(recoveryBackoff(generation - 1) == 0, 47)
    }
    collect(client as UWord)
    expect(terminateTask(peer as UWord, 0) == 0, 48)
    collect(peer as UWord)
    expect(closeHandle(control as UWord) == 0, 49)
    recoveryDone()
    exit(0)
}
export { recoveryPolicyMain }

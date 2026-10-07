// Acceptance image: a display chain killed mid-frame five times (four real
// faults, the fifth generation lives), explicit reconnects, an Echo service and a
// CPU-bound peer that outlive every Screen generation.
import { RuntimeStart, TaskEvent } from "../../../src/task/runtime_start.m"
import { ServiceResolution } from "../../../src/task/recovery_start.m"
import { ManagedService, launchService, retireService, recoveryBackoff } from "../../../user/recovery/policy.m"
import { screenWriteFor } from "../../../user/screen/client.m"
import { createTask, configureTask, publishTask, terminateTask, inspectTask,
    collectTask, allowServices, createEndpoint, closeHandle, resolveService,
    callTimed, send, recv, tryRecv, sleep, yield, exit } from "../../../user/syscalls.m"
import { ENDPOINT_MODE_RAW, RIGHT_SEND, RIGHT_RECEIVE, DEVICE_SCREEN, DEVICE_FONT,
    ERRNO_EPIPE, ERRNO_EAGAIN, TASK_EVENT_RECLAIMED } from "../../../src/arch/wrm081632/defs.m"
extern let recoveryDone(): Void
extern let screenRendered(generation: UWord): Void
let mut message: UWord[8]
let mut answer: UWord[8]
let mut text: UByte[4]
let mut resolution: ServiceResolution
let mut event: TaskEvent
let mut echo: ManagedService
let mut bitmap: ManagedService
let mut screen: ManagedService
let expect(ok: Bool, code: Word): Void { if !ok exit(code) }
let clientMain(start: *RuntimeStart): Void {
    let mut old: UWord = 0
    let mut echoHandle: UWord = 0
    let mut previousInstance: UWord = 0
    for generation: UWord in 1..6 {
        expect(recv(start.endpoint, &mut message[0] as *mut UByte, 4) == 4 && message[0] == generation, 12)
        expect(resolveService(1, &mut resolution) == 0 && resolution.generation == generation, 13)
        let fresh: UWord = resolution.handle
        expect(resolution.instance != previousInstance, 14)
        previousInstance = resolution.instance
        if echoHandle == 0 {
            expect(resolveService(3, &mut resolution) == 0 && resolution.generation == 1, 15)
            echoHandle = resolution.handle
        }
        // The previous generation's handle stays until the fresh one is installed.
        if old != 0 {
            text[0] = 'x' as UByte
            expect(screenWriteFor(old, &text[0] as *UByte, 1, 5) == -ERRNO_EPIPE, 16)
            expect(closeHandle(old) == 0, 17)
        }
        text[0] = 'G' as UByte
        text[1] = (48 + generation) as UByte
        expect(screenWriteFor(fresh, &text[0] as *UByte, 2, 5) == 2, 18)
        screenRendered(generation)
        message[0] = generation
        expect(callTimed(echoHandle, &message[0] as *UByte, 4, &mut answer[0] as *mut UByte, 32, 5) == 4 && answer[0] == generation, 19)
        if generation < 5 {
            // The server renders this glyph, then faults with our reply right held.
            text[0] = '!' as UByte
            expect(screenWriteFor(fresh, &text[0] as *UByte, 1, 5) == -ERRNO_EPIPE, 20)
        }
        old = fresh
        expect(send(start.endpoint, &message[0] as *UByte, 4) == 4, 21)
    }
    expect(closeHandle(old) == 0 && closeHandle(echoHandle) == 0, 22)
    exit(0)
}
let waitClient(control: UWord, client: UWord, generation: UWord): Void {
    for wait: UWord in 0..20 {
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
let screenScenarioMain(start: *RuntimeStart, bytes: UWord): Void {
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
    expect(control > 0 && client > 0 && allowServices(client as UWord, 5) == 0, 36)
    expect(configureTask(client as UWord, control as UWord, RIGHT_SEND | RIGHT_RECEIVE, 1) == 0 && publishTask(client as UWord) == 0, 37)
    let peer: Word = createTask(5)
    expect(peer > 0 && configureTask(peer as UWord, 0, 0, 2) == 0 && publishTask(peer as UWord) == 0, 38)
    expect(launchService(&mut echo, 2, 3, 0, 1, 0) == 0, 39)
    for generation: UWord in 1..6 {
        expect(launchService(&mut bitmap, 3, 2, 0, generation, DEVICE_FONT) == 0, 40)
        expect(launchService(&mut screen, 4, 1, bitmap.root, generation, DEVICE_SCREEN) == 0, 41)
        message[0] = generation
        expect(send(control as UWord, &message[0] as *UByte, 4) == 4, 42)
        waitClient(control as UWord, client as UWord, generation)
        // Consumer retires before its producer; replacements launch in reverse.
        expect(retireService(&mut screen, 1) == 0, 43)
        expect(retireService(&mut bitmap, 2) == 0, 44)
        expect(inspectTask(peer as UWord, &mut event) == 0 && event.state != 3, 46)
        expect(inspectTask(echo.reference, &mut event) == 0 && event.state != 3, 47)
        if generation < 5 expect(recoveryBackoff(generation - 1) == 0, 48)
    }
    collect(client as UWord)
    expect(retireService(&mut echo, 3) == 0, 49)
    expect(terminateTask(peer as UWord, 0) == 0, 50)
    collect(peer as UWord)
    expect(closeHandle(control as UWord) == 0, 51)
    recoveryDone()
    exit(0)
}
export { screenScenarioMain }

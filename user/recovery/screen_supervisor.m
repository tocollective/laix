// Private policy for a supervised display: bitmap storage -> Screen, with an
// independent Echo peer. Names are supervisor-local: 1 Screen, 2 bitmap storage,
// 3 Echo. Images: 2 Echo, 3 bitmap, 4 Screen, 5 this supervisor/client.
// The display chain follows the Files/Disk rules: five constructions per boot,
// 1/2/4/8 second backoff, consumer retired before producer, explicit reconnect.
import { RuntimeStart } from "../../src/task/runtime_start.m"
import { ServiceResolution } from "../../src/task/recovery_start.m"
import { ManagedService, launchService, serviceFailed, recoverService,
    recoverScreenBitmap, recoveryUnavailable } from "policy.m"
import { screenWriteFor } from "../screen/client.m"
import { createTask, configureTask, publishTask, allowServices, createEndpoint,
    resolveService, closeHandle, callTimed, tryAccept, reply, AcceptResult, sleep, exit, discard } from "../syscalls.m"
import { ENDPOINT_MODE_SERVICE, RIGHT_SEND, DEVICE_SCREEN, DEVICE_FONT } from "../../src/arch/wrm081632/defs.m"
import { IMAGE_REC_ECHO, IMAGE_REC_BITMAP, IMAGE_REC_SCREEN, IMAGE_REC_SCREEN_CLIENT } from "../init/images.m"
let mut echo: ManagedService
let mut bitmap: ManagedService
let mut screen: ManagedService
let mut resolution: ServiceResolution
let mut request: UWord[2]
let mut response: UWord[8]
let mut report: UWord[2]
let mut accepted: AcceptResult
let mut line: UByte[16]
let check(result: Word): Void { if result != 0 exit(result) }

// "\rLA/IX nnnnnn gG": CR rewrites one status line instead of scrolling.
let statusLine(tick: UWord, generation: UWord): Void {
    line[0] = 13
    line[1] = 'L' as UByte
    line[2] = 'A' as UByte
    line[3] = '/' as UByte
    line[4] = 'I' as UByte
    line[5] = 'X' as UByte
    line[6] = ' ' as UByte
    let mut value: UWord = tick
    for digit: UWord in 0..6 {
        line[12 - digit] = (48 + value % 10) as UByte
        value = value / 10
    }
    line[13] = ' ' as UByte
    line[14] = 'g' as UByte
    line[15] = (48 + generation % 10) as UByte
}

// Reports a failed incarnation (with its resource generation) and drops the
// handle; the next pass resolves again. Nothing is rebound on the client's behalf.
let reportFailed(control: UWord, name: UWord, generation: UWord): Void {
    report[0] = name
    report[1] = generation
    discard(callTimed(control, &report[0] as *UByte, 8, &mut response[0] as *mut UByte, 32, 5))
}
let screenClient(start: *RuntimeStart): Void {
    let mut screenHandle: UWord = 0
    let mut echoHandle: UWord = 0
    let mut screenGeneration: UWord = 0
    let mut echoGeneration: UWord = 0
    let mut tick: UWord = 0
    while true {
        if screenHandle == 0 && resolveService(1, &mut resolution) == 0 {
            screenHandle = resolution.handle
            screenGeneration = resolution.generation
        }
        if echoHandle == 0 && resolveService(3, &mut resolution) == 0 {
            echoHandle = resolution.handle
            echoGeneration = resolution.generation
        }
        if screenHandle != 0 {
            statusLine(tick, screenGeneration)
            if screenWriteFor(screenHandle, &line[0] as *UByte, 16, 8) != 16 {
                reportFailed(start.endpoint, 1, screenGeneration)
                check(closeHandle(screenHandle))
                screenHandle = 0
            }
        }
        if echoHandle != 0 {
            request[0] = echoGeneration
            let replied: Word = callTimed(echoHandle, &request[0] as *UByte, 4,
                &mut response[0] as *mut UByte, 32, 5)
            if replied != 4 || response[0] != echoGeneration {
                reportFailed(start.endpoint, 3, echoGeneration)
                check(closeHandle(echoHandle))
                echoHandle = 0
            }
        }
        tick += 1
        check(sleep(1))
    }
}
// Entry of the client image.
let screenClientMain(start: *RuntimeStart, bytes: UWord): Void {
    screenClient(start)
}

// The supervisor itself: init runs this as its supervised-display session.
let screenSupervisorRun(): Void {
    let control: Word = createEndpoint(ENDPOINT_MODE_SERVICE, 0)
    if control < 0 exit(control)
    check(launchService(&mut echo, IMAGE_REC_ECHO, 3, 0, 1, 0))
    check(launchService(&mut bitmap, IMAGE_REC_BITMAP, 2, 0, 1, DEVICE_FONT))
    check(launchService(&mut screen, IMAGE_REC_SCREEN, 1, bitmap.root, 1, DEVICE_SCREEN))
    let client: Word = createTask(IMAGE_REC_SCREEN_CLIENT)
    if client < 0 exit(client)
    check(allowServices(client as UWord, 5))
    check(configureTask(client as UWord, control as UWord, RIGHT_SEND, 1))
    check(publishTask(client as UWord))
    while true {
        // The sender is the one configured child on this private control endpoint.
        let size: Word = tryAccept(control as UWord, &mut report[0] as *mut UByte, 8, &mut accepted)
        if size >= 0 {
            discard(reply(accepted.replyToken, &report[0] as *UByte, size as UWord))
        }
        let screenReported: Bool = size == 8 && report[0] == 1 && report[1] == screen.generation
        let echoReported: Bool = size == 8 && report[0] == 3 && report[1] == echo.generation
        if !echo.unavailable && (echoReported || serviceFailed(&echo)) {
            let recovered: Word = recoverService(&mut echo, IMAGE_REC_ECHO, 3, 0, 0)
            if recovered != 0 {
                discard(recoveryUnavailable(&mut echo, 3))
            }
        }
        if !screen.unavailable && (screenReported || serviceFailed(&bitmap) || serviceFailed(&screen)) {
            let recovered: Word = recoverScreenBitmap(&mut bitmap, &mut screen)
            if recovered != 0 {
                discard(recoveryUnavailable(&mut screen, 1))
            }
        }
        check(sleep(1))
    }
}
export { screenSupervisorRun, screenClientMain }

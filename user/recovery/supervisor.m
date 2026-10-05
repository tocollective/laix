// User policy for a private Echo and immutable Files/Disk service group.
import { RuntimeStart } from "../../src/task/runtime_start.m"
import { ServiceResolution } from "../../src/task/recovery_start.m"
import { ManagedService, launchService, serviceFailed, recoverService,
    recoverFilesDisk, recoveryUnavailable } from "policy.m"
import { createTask, configureTask, publishTask, allowServices, createEndpoint,
    resolveService, closeHandle, callTimed, tryAccept, reply, AcceptResult, sleep, exit } from "../syscalls.m"
import { ENDPOINT_MODE_SERVICE, RIGHT_SEND, RIGHT_RECEIVE, DEVICE_DISK,
    ERRNO_EPIPE, ERRNO_EAGAIN, FILE_REQUEST_HEADER, FILE_RESPONSE_HEADER,
    FILE_FONT_ID } from "../../src/arch/wrm081632/defs.m"
let mut echo: ManagedService
let mut disk: ManagedService
let mut files: ManagedService
let mut resolution: ServiceResolution
let mut request: UWord[5]
let mut response: UWord[8]
let mut report: UWord[2]
let mut accepted: AcceptResult
let check(result: Word): Void { if result != 0 exit(result) }

// Resolution is explicit on first use and after EPIPE/timeout/protocol failure.
// Only reads/echo are retried; a future side-effecting protocol needs request IDs.
let recoveryClient(start: *RuntimeStart): Void {
    let mut echoHandle: UWord = 0
    let mut fileHandle: UWord = 0
    let mut echoGeneration: UWord = 0
    let mut fileGeneration: UWord = 0
    while true {
        if echoHandle == 0 && resolveService(1, &mut resolution) == 0 {
            echoHandle = resolution.handle
            echoGeneration = resolution.generation
        }
        if fileHandle == 0 && resolveService(2, &mut resolution) == 0 {
            fileHandle = resolution.handle
            fileGeneration = resolution.generation
        }
        if echoHandle != 0 {
            request[0] = echoGeneration
            let replied: Word = callTimed(echoHandle, &request[0] as *UByte, 4,
                &mut response[0] as *mut UByte, 32, 5)
            if replied != 4 || response[0] != echoGeneration {
                report[0] = 1
                report[1] = echoGeneration
                let notified: Word = callTimed(start.endpoint, &report[0] as *UByte, 8, &mut response[0] as *mut UByte, 32, 5)
                check(closeHandle(echoHandle))
                echoHandle = 0
            }
        }
        if fileHandle != 0 {
            request[0] = FILE_REQUEST_HEADER
            request[1] = fileGeneration
            request[2] = FILE_FONT_ID
            request[3] = 0
            request[4] = 16
            let replied: Word = callTimed(fileHandle, &request[0] as *UByte, 20,
                &mut response[0] as *mut UByte, 32, 5)
            if replied != 32 || response[0] != FILE_RESPONSE_HEADER ||
                response[1] != 0 || response[2] != fileGeneration || response[3] != 16 {
                report[0] = 2
                report[1] = fileGeneration
                let notified: Word = callTimed(start.endpoint, &report[0] as *UByte, 8, &mut response[0] as *mut UByte, 32, 5)
                check(closeHandle(fileHandle))
                fileHandle = 0
            }
        }
        check(sleep(1))
    }
}
let recoverySupervisorMain(start: *RuntimeStart, bytes: UWord): Void {
    if start.argument == 1 recoveryClient(start)
    let control: Word = createEndpoint(ENDPOINT_MODE_SERVICE, 0)
    if control < 0 exit(control)
    check(launchService(&mut echo, 2, 1, 0, 1, 0))
    check(launchService(&mut disk, 3, 3, 0, 1, DEVICE_DISK))
    check(launchService(&mut files, 4, 2, disk.root, 1, 0))
    let client: Word = createTask(5)
    if client < 0 exit(client)
    check(allowServices(client as UWord, 3))
    check(configureTask(client as UWord, control as UWord, RIGHT_SEND, 1))
    check(publishTask(client as UWord))
    while true {
        // The sender is the one configured child on this private control endpoint.
        let size: Word = tryAccept(control as UWord, &mut report[0] as *mut UByte, 8, &mut accepted)
        if size >= 0 {
            let acknowledged: Word = reply(accepted.replyToken, &report[0] as *UByte, size as UWord)
        }
        let echoReported: Bool = size == 8 && report[0] == 1 && report[1] == echo.generation
        let filesReported: Bool = size == 8 && report[0] == 2 && report[1] == files.generation
        if !echo.unavailable && (echoReported || serviceFailed(&echo)) {
            let recovered: Word = recoverService(&mut echo, 2, 1, 0, 0)
            if recovered != 0 {
                let disabled: Word = recoveryUnavailable(&mut echo, 1)
            }
        }
        if !files.unavailable && (filesReported || serviceFailed(&disk) || serviceFailed(&files)) {
            let recovered: Word = recoverFilesDisk(&mut disk, &mut files)
            if recovered != 0 {
                let disabled: Word = recoveryUnavailable(&mut files, 2)
            }
        }
        check(sleep(1))
    }
}
export { recoverySupervisorMain }

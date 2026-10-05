// Stateless production services: immutable boot data, no persisted user state.
import { RecoveryStart } from "../../src/task/recovery_start.m"
import { diskHandle, diskSetGeneration, diskDependencyFailed } from "../services/disk.m"
import { fileHandle, filesSetGeneration, filesDependencyFailed } from "../services/files.m"
import { accept, reply, exit, irqComplete, AcceptResult } from "../syscalls.m"
import { RECOVERY_START_BYTES, RUNTIME_START_MAGIC } from "../../src/arch/wrm081632/defs.m"
let mut request: UWord[8]
let mut response: UWord[8]
let valid(start: *RecoveryStart, bytes: UWord): Void {
    if bytes != RECOVERY_START_BYTES || start.magic != RUNTIME_START_MAGIC ||
        start.generation == 0 || start.reference == 0 exit(1)
}
let echoMain(start: *RecoveryStart, bytes: UWord): Void {
    valid(start, bytes)
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(start.endpoint, &mut request[0] as *mut UByte, 32, &mut accepted)
        if size < 0 exit(2)
        let sent: Word = reply(accepted.replyToken, &request[0] as *UByte, size as UWord)
        if sent > 32 exit(3)
    }
}
let recoveryDiskMain(start: *RecoveryStart, bytes: UWord): Void {
    valid(start, bytes)
    diskSetGeneration(start.generation)
    if irqComplete(start.irq) != 0 exit(4)
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(start.endpoint, &mut request[0] as *mut UByte, 32, &mut accepted)
        if size < 0 exit(5)
        diskHandle(&request[0], size as UWord, &mut response[0], start.irq)
        let sent: Word = reply(accepted.replyToken, &response[0] as *UByte, 32)
        if sent > 32 exit(6)
        if diskDependencyFailed() exit(7)
    }
}
let recoveryFilesMain(start: *RecoveryStart, bytes: UWord): Void {
    valid(start, bytes)
    filesSetGeneration(start.generation)
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(start.endpoint, &mut request[0] as *mut UByte, 32, &mut accepted)
        if size < 0 exit(8)
        fileHandle(&request[0], size as UWord, &mut response[0], start.dependency)
        let sent: Word = reply(accepted.replyToken, &response[0] as *UByte, 32)
        if sent > 32 exit(9)
        if filesDependencyFailed() exit(10)
    }
}
export { echoMain, recoveryDiskMain, recoveryFilesMain }

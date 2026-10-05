// CPU-only crash fixtures. Production protocol helpers have no crash command.
import { RecoveryStart } from "../../../src/task/recovery_start.m"
import { diskHandle, diskSetGeneration, diskDependencyFailed } from "../../../user/services/disk.m"
import { fileHandle, filesSetGeneration, filesDependencyFailed } from "../../../user/services/files.m"
import { accept, reply, exit, irqComplete, AcceptResult } from "../../../user/syscalls.m"
import { RECOVERY_START_BYTES, RUNTIME_START_MAGIC, DISK_REQUEST_HEADER } from "../../../src/arch/wrm081632/defs.m"
extern let recoveryFault(): Void
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
        if size == 4 && request[0] == 0xDEAD recoveryFault()
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
        // Fault after accepting the Files read, with a live reply right.
        if request[0] == DISK_REQUEST_HEADER && request[2] == 32 && start.generation < 5 recoveryFault()
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

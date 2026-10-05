// One immutable file (/font, ID 1). No paths, writes, allocation or mounts.
import { ServiceStart, serviceStartValid } from "../../src/task/service_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_FILE, DATA_GENERATION,
    FILE_FONT_ID, FILE_REQUEST_HEADER, FILE_STAT_HEADER, FILE_RESPONSE_HEADER,
    DISK_REQUEST_HEADER, DISK_STAT_HEADER, DISK_RESPONSE_HEADER, SECTOR_SIZE,
    ERRNO_EINVAL, ERRNO_ENOENT, ERRNO_EPIPE, ERRNO_EPROTO } from "../../src/arch/wrm081632/defs.m"
import { AcceptResult, accept, reply, call, callTimed, exit } from "../syscalls.m"
let mut fileRequest: UWord[8]
let mut fileResponse: UWord[8]
let mut fileDiskRequest: UWord[4]
let mut fileDiskResponse: UWord[8]
let mut filesGeneration: UWord = DATA_GENERATION
let mut filesTimed: Bool
let mut filesFailed: Bool

let fileDiskCall(disk: UWord, header: UWord, offset: UWord, count: UWord): Word {
    fileDiskRequest[0] = header
    fileDiskRequest[1] = filesGeneration
    fileDiskRequest[2] = offset
    fileDiskRequest[3] = count
    let mut size: Word = 0
    if filesTimed size = callTimed(disk, &fileDiskRequest[0] as *UByte, 16, &mut fileDiskResponse[0] as *mut UByte, 32, 5)
    else size = call(disk, &fileDiskRequest[0] as *UByte, 16, &mut fileDiskResponse[0] as *mut UByte, 32)
    if size < 0 {
        filesFailed = true
        return size
    }
    if size != 32 || fileDiskResponse[0] != DISK_RESPONSE_HEADER ||
        fileDiskResponse[2] != filesGeneration return -ERRNO_EPROTO
    let status: Word = fileDiskResponse[1] as Word
    if status > 0 || (status < 0 && fileDiskResponse[3] != 0) return -ERRNO_EPROTO
    if status != 0 return status
    if header == DISK_REQUEST_HEADER && fileDiskResponse[3] != count return -ERRNO_EPROTO
    return 0
}

let fileHandle(request: *UWord, size: UWord, response: *mut UWord, disk: UWord): Void {
    for i: UWord in 0..8 response[i] = 0
    response[0] = FILE_RESPONSE_HEADER
    response[1] = (-ERRNO_EINVAL) as UWord
    response[2] = filesGeneration
    if size != 16 && size != 20 return
    let stat: Bool = size == 16 && request[0] == FILE_STAT_HEADER
    let read: Bool = size == 20 && request[0] == FILE_REQUEST_HEADER
    if !stat && !read return
    if stat && request[3] != 0 return
    if read && (request[4] == 0 || request[4] > 16) return
    if request[1] != filesGeneration {
        response[1] = (-ERRNO_EPIPE) as UWord
        return
    }
    if request[2] != FILE_FONT_ID {
        response[1] = (-ERRNO_ENOENT) as UWord
        return
    }
    // Even stat and EOF validate the medium; cached size is not liveness.
    let mut status: Word = fileDiskCall(disk, DISK_STAT_HEADER, 0, 0)
    if status != 0 {
        response[1] = status as UWord
        filesFailed = true
        return
    }
    let extent: UWord = fileDiskResponse[3]
    if extent == 0 || extent > 0x7FFFFFFF || extent % 32 != 0 {
        response[1] = (-ERRNO_EPROTO) as UWord
        filesFailed = true
        return
    }
    if stat {
        response[1] = 0
        response[3] = extent
        return
    }
    if request[3] > extent return
    if request[3] == extent {
        response[1] = 0
        return
    }
    let mut count: UWord = request[4]
    if count > extent - request[3] count = extent - request[3]
    let sectorRemaining: UWord = SECTOR_SIZE - request[3] % SECTOR_SIZE
    if count > sectorRemaining count = sectorRemaining
    status = fileDiskCall(disk, DISK_REQUEST_HEADER, request[3], count)
    response[1] = status as UWord
    if status != 0 {
        filesFailed = true
        return
    }
    response[3] = count
    let source: *UByte = &fileDiskResponse[4] as *UByte
    let destination: *mut UByte = &mut response[4] as *mut UByte
    for i: UWord in 0..count destination[i] = source[i]
}

let filesMain(start: *ServiceStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != SERVICE_START_BYTES ||
        !serviceStartValid(start) || start.role != START_ROLE_FILE) exit(1)
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(start.endpoint, &mut fileRequest[0] as *mut UByte, 32, &mut accepted)
        if size < 0 exit(1)
        fileHandle(&fileRequest[0], size as UWord, &mut fileResponse[0], start.bitmapEndpoint)
        let sent: Word = reply(accepted.replyToken, &fileResponse[0] as *UByte, 32)
        if sent > 32 exit(1)
        if filesFailed exit(1)
    }
}
let filesSetGeneration(generation: UWord): Void { filesGeneration = generation
    filesTimed = true }
let filesDependencyFailed(): Bool { return filesFailed }
export { filesDependencyFailed, filesSetGeneration, fileHandle, fileDiskCall, filesMain }

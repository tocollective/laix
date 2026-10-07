// Bounded clients of the WFS1 filesystem service (docs/FILESYSTEM.md). Every
// response is checked before anything is published to the caller. A file is
// named by an id from fsOpen; the id goes stale when the file is deleted or
// replaced, and a stale id answers -ENOENT.
import { FS_OPEN_HEADER, FS_STAT_HEADER, FS_READ_HEADER, FS_WRITE_HEADER, FS_SYNC_HEADER,
    FS_DELETE_HEADER, FS_LIST_HEADER, FS_INFO_HEADER, FS_RESPONSE_HEADER, FS_NAME_BYTES,
    FS_CREATE, FS_TRUNC, DATA_GENERATION, ERRNO_EINVAL, ERRNO_EPROTO } from "../../src/arch/wrm081632/defs.m"
import { call } from "../syscalls.m"

type FsFile {
    id: UWord,
    size: UWord,
    capacity: UWord, // bytes
}
type FsInfo {
    dataSectors: UWord,
    freeSectors: UWord,
    generation: UWord,
    flags: UWord, // 1 writable, 2 metadata pending, 4 data pending
}

let mut fsClientRequest: UWord[8]
let mut fsClientResponse: UWord[8]

// Messages are word arrays on both sides; bytes move through shifts.
let fsClientByte(words: *UWord, index: UWord): UWord {
    return (words[index >> 2] >> ((index & 3) << 3)) & 255
}
let fsClientPut(words: *mut UWord, index: UWord, value: UWord): Void {
    let shift: UWord = (index & 3) << 3
    words[index >> 2] = (words[index >> 2] & ~(255 << shift)) | (value << shift)
}

let fsClientSend(handle: UWord, header: UWord, a: UWord, b: UWord, size: UWord): Word {
    fsClientRequest[0] = header
    fsClientRequest[1] = DATA_GENERATION
    fsClientRequest[2] = a
    fsClientRequest[3] = b
    let got: Word = call(handle, &fsClientRequest[0] as *UByte, size, &mut fsClientResponse[0] as *mut UByte, 32)
    if got < 0 return got
    if got != 32 || fsClientResponse[0] != FS_RESPONSE_HEADER || fsClientResponse[2] != DATA_GENERATION return -ERRNO_EPROTO
    let status: Word = fsClientResponse[1] as Word
    if status > 0 || (status < 0 && fsClientResponse[3] != 0) return -ERRNO_EPROTO
    return status
}

// Packs a NUL-terminated name of 1..16 bytes into request words 4..7.
let fsClientName(name: *UByte): Bool {
    for w: UWord in 4..8 fsClientRequest[w] = 0
    let mut length: UWord = 0
    while name[length] != 0 {
        if length == FS_NAME_BYTES return false
        fsClientPut(&mut fsClientRequest[4], length, name[length] as UWord)
        length += 1
    }
    return length != 0
}

let fsClientFile(file: *mut FsFile): Void {
    if file == null return
    file.id = fsClientResponse[3]
    file.size = fsClientResponse[4]
    file.capacity = fsClientResponse[5]
}

// flags: FS_CREATE, FS_EXCL, FS_TRUNC. capacity is in sectors and is used only
// when a file is created or replaced.
let fsOpen(handle: UWord, name: *UByte, flags: UWord, capacity: UWord, file: *mut FsFile): Word {
    if name == null || !fsClientName(name) return -ERRNO_EINVAL
    let status: Word = fsClientSend(handle, FS_OPEN_HEADER, flags, capacity, 32)
    if status == 0 fsClientFile(file)
    return status
}

let fsStat(handle: UWord, id: UWord, file: *mut FsFile): Word {
    let status: Word = fsClientSend(handle, FS_STAT_HEADER, id, 0, 16)
    if status != 0 return status
    if file != null {
        file.id = id
        file.size = fsClientResponse[3]
        file.capacity = fsClientResponse[4]
    }
    return 0
}

// At most 16 bytes, to the end of the file or of the sector. Returns the count.
let fsReadAt(handle: UWord, id: UWord, offset: UWord, destination: *mut UByte, bytes: UWord): Word {
    if destination == null || bytes == 0 || bytes > 16 return -ERRNO_EINVAL
    fsClientRequest[4] = bytes
    let status: Word = fsClientSend(handle, FS_READ_HEADER, id, offset, 20)
    if status != 0 return status
    let count: UWord = fsClientResponse[3]
    if count > bytes return -ERRNO_EPROTO
    for i: UWord in 0..count destination[i] = fsClientByte(&fsClientResponse[4], i) as UByte
    return count as Word
}

// At most 16 bytes at an offset not beyond the end of the file. Returns the count
// taken, which stops at the end of the sector and of the file's capacity.
let fsWriteAt(handle: UWord, id: UWord, offset: UWord, source: *UByte, bytes: UWord): Word {
    if source == null || bytes == 0 || bytes > 16 return -ERRNO_EINVAL
    for w: UWord in 4..8 fsClientRequest[w] = 0
    for i: UWord in 0..bytes fsClientPut(&mut fsClientRequest[4], i, source[i] as UWord)
    let status: Word = fsClientSend(handle, FS_WRITE_HEADER, id, offset, 16 + bytes)
    if status != 0 return status
    let count: UWord = fsClientResponse[3]
    if count == 0 || count > bytes return -ERRNO_EPROTO
    return count as Word
}

// Makes everything written so far durable (see FILESYSTEM.md for the order).
let fsSync(handle: UWord): Word {
    return fsClientSend(handle, FS_SYNC_HEADER, 0, 0, 16)
}

let fsRemove(handle: UWord, name: *UByte): Word {
    if name == null || !fsClientName(name) return -ERRNO_EINVAL
    return fsClientSend(handle, FS_DELETE_HEADER, 0, 0, 32)
}

// The index-th file: its size, and its name NUL padded into 16 bytes at `name`.
let fsListAt(handle: UWord, index: UWord, name: *mut UByte, size: *mut UWord): Word {
    if name == null || size == null return -ERRNO_EINVAL
    let status: Word = fsClientSend(handle, FS_LIST_HEADER, index, 0, 16)
    if status != 0 return status
    size[0] = fsClientResponse[3]
    for i: UWord in 0..FS_NAME_BYTES name[i] = fsClientByte(&fsClientResponse[4], i) as UByte
    return 0
}

let fsInfo(handle: UWord, info: *mut FsInfo): Word {
    if info == null return -ERRNO_EINVAL
    let status: Word = fsClientSend(handle, FS_INFO_HEADER, 0, 0, 16)
    if status != 0 return status
    info.dataSectors = fsClientResponse[3]
    info.freeSectors = fsClientResponse[4]
    info.generation = fsClientResponse[5]
    info.flags = fsClientResponse[6]
    return 0
}

// The whole file into buffer[0..size). Returns the size, -EINVAL when it does not
// fit, or -EPROTO for a short or inconsistent read.
let fsReadAll(handle: UWord, id: UWord, buffer: *mut UByte, capacity: UWord): Word {
    let mut file: FsFile
    let status: Word = fsStat(handle, id, &mut file)
    if status != 0 return status
    if file.size > capacity return -ERRNO_EINVAL
    let mut offset: UWord = 0
    while offset < file.size {
        let mut piece: UWord = file.size - offset
        if piece > 16 piece = 16
        let count: Word = fsReadAt(handle, id, offset, &mut buffer[offset], piece)
        if count < 0 return count
        if count == 0 return -ERRNO_EPROTO
        offset += count as UWord
    }
    return file.size as Word
}

// Writes bytes at `offset` (at most the file size), in as many requests as the
// service needs. Returns zero or the first error; nothing is durable until fsSync.
let fsWriteAll(handle: UWord, id: UWord, offset: UWord, source: *UByte, bytes: UWord): Word {
    let mut done: UWord = 0
    while done < bytes {
        let mut piece: UWord = bytes - done
        if piece > 16 piece = 16
        let count: Word = fsWriteAt(handle, id, offset + done, &source[done], piece)
        if count < 0 return count
        done += count as UWord
    }
    return 0
}

// Opens a file by name and reads all of it. Returns the size or a negative errno.
let fsReadFile(handle: UWord, name: *UByte, buffer: *mut UByte, capacity: UWord): Word {
    let mut file: FsFile
    let status: Word = fsOpen(handle, name, 0, 0, &mut file)
    if status != 0 return status
    return fsReadAll(handle, file.id, buffer, capacity)
}

// Replaces or creates a file with exactly these bytes; not durable until fsSync.
let fsPut(handle: UWord, name: *UByte, source: *UByte, bytes: UWord): Word {
    let mut file: FsFile
    let mut sectors: UWord = (bytes + 511) / 512
    if sectors == 0 sectors = 1
    let mut status: Word = fsOpen(handle, name, FS_CREATE, sectors, &mut file)
    if status != 0 return status
    if bytes > file.capacity || file.size != 0 {
        status = fsOpen(handle, name, FS_TRUNC, sectors, &mut file)
        if status != 0 return status
    }
    return fsWriteAll(handle, file.id, 0, source, bytes)
}

// fsPut, then the commit.
let fsWriteFile(handle: UWord, name: *UByte, source: *UByte, bytes: UWord): Word {
    let status: Word = fsPut(handle, name, source, bytes)
    if status != 0 return status
    return fsSync(handle)
}

export { FsFile, FsInfo, fsOpen, fsStat, fsReadAt, fsWriteAt, fsSync, fsRemove, fsListAt, fsInfo,
    fsReadAll, fsWriteAll, fsReadFile, fsPut, fsWriteFile }

// WFS1: a flat, writable filesystem over the Disk service's sector stage
// (docs/FILESYSTEM.md). Files are contiguous extents of whole sectors with a
// capacity fixed at creation. Two metadata copies and a checksum make a commit
// atomic: the copy that is not current is written with generation + 1, so a
// torn commit leaves the previous copy valid. Data is written before the commit
// that refers to it and flushed in between; space freed since the last commit is
// not reused until the next one. Overwrites of committed bytes are in place and
// are not atomic. No paths, no directories, no permissions.
import { ServiceStart, serviceStartValid } from "../../src/task/service_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_FILE, DATA_GENERATION,
    FS_OPEN_HEADER, FS_STAT_HEADER, FS_READ_HEADER, FS_WRITE_HEADER, FS_SYNC_HEADER,
    FS_DELETE_HEADER, FS_LIST_HEADER, FS_INFO_HEADER, FS_RESPONSE_HEADER, FS_CREATE, FS_EXCL,
    FS_TRUNC, FS_MAX_FILES, FS_DATA_START, FS_MAGIC, EXTENT_WRITE,
    DISK_STAT_HEADER, DISK_RESPONSE_HEADER, DISK_LOAD_HEADER, DISK_PEEK_HEADER,
    DISK_POKE_HEADER, DISK_STORE_HEADER, DISK_SYNC_HEADER, DISK_FLAGS_HEADER, SECTOR_SIZE,
    ERRNO_EINVAL, ERRNO_ENOENT, ERRNO_EPIPE, ERRNO_EPROTO, ERRNO_EIO, ERRNO_EROFS,
    ERRNO_ENOSPC, ERRNO_EEXIST, ERRNO_ENODEV } from "../../src/arch/wrm081632/defs.m"
import { AcceptResult, accept, reply, call, callTimed, exit } from "../syscalls.m"

// Metadata copy layout, in words. See tools/wfs.py for the reference.
let H_MAGIC: UWord = 0
let H_GENERATION: UWord = 1
let H_SERIAL: UWord = 2
let H_SECTORS: UWord = 3
let H_DATA: UWord = 4
let H_CRC: UWord = 5
let H_RESERVED: UWord = 6
let HEADER_WORDS: UWord = 8
let ENTRY_WORDS: UWord = 8
let E_SIZE: UWord = 4
let E_FIRST: UWord = 5
let E_CAPACITY: UWord = 6
let E_SERIAL: UWord = 7
let TABLE_WORDS: UWord = 256
let SECTOR_WORDS: UWord = 128
let NO_SECTOR: UWord = 0xFFFFFFFF
let SLOT_NONE: UWord = 31
let MAX_SERIAL: UWord = 0x3FFFFFF
let FS_MIN_SECTORS: UWord = 5

let mut fsRequest: UWord[8]
let mut fsResponse: UWord[8]
let mut fsDiskRequest: UWord[8]
let mut fsDiskResponse: UWord[8]
let mut fsTable: UWord[256] // working metadata
let mut fsCommitted: UWord[256] // the copy on the medium that is current
let mut fsScratch: UWord[256] // the other copy while mounting
let mut fsBuffer: UWord[128] // one data sector, written back lazily
let mut fsBufferSector: UWord = 0xFFFFFFFF
let mut fsBufferDirty: Bool
// The sector whose bytes the Disk stage holds, if known. A read that is not
// served from the dirty buffer peeks the stage directly instead of copying a
// whole sector into the buffer first.
let mut fsStageSector: UWord = 0xFFFFFFFF
let mut fsDataDirty: Bool // sectors stored since the last Disk flush
let mut fsMetaDirty: Bool // the working table differs from the committed one
let mut fsMounted: Bool
let mut fsWritable: Bool
let mut fsActive: UWord // 0 or 1: the copy holding the committed generation
let mut fsGeneration: UWord = DATA_GENERATION
let mut fsTimed: Bool
let mut fsFailed: Bool

// CRC-32 (IEEE) of a metadata copy with its checksum word taken as zero.
let fsCrc(table: *UWord): UWord {
    let mut crc: UWord = 0xFFFFFFFF
    for w: UWord in 0..TABLE_WORDS {
        let mut word: UWord = table[w]
        if w == H_CRC word = 0
        for b: UWord in 0..4 {
            crc = crc ^ (word & 255)
            word = word >> 8
            for k: UWord in 0..8 {
                if crc & 1 != 0 crc = (crc >> 1) ^ 0xEDB88320
                else crc = crc >> 1
            }
        }
    }
    return crc ^ 0xFFFFFFFF
}

// ---- Disk service calls -------------------------------------------------

let fsDiskCall(disk: UWord, size: UWord): Word {
    let mut got: Word = 0
    if fsTimed got = callTimed(disk, &fsDiskRequest[0] as *UByte, size, &mut fsDiskResponse[0] as *mut UByte, 32, 5)
    else got = call(disk, &fsDiskRequest[0] as *UByte, size, &mut fsDiskResponse[0] as *mut UByte, 32)
    if got < 0 {
        fsFailed = true
        return got
    }
    if got != 32 || fsDiskResponse[0] != DISK_RESPONSE_HEADER || fsDiskResponse[2] != fsGeneration return -ERRNO_EPROTO
    let status: Word = fsDiskResponse[1] as Word
    if status > 0 || (status < 0 && fsDiskResponse[3] != 0) return -ERRNO_EPROTO
    if status == -ERRNO_EPIPE || status == -ERRNO_EIO fsFailed = true
    return status
}

let fsDisk(disk: UWord, header: UWord, a: UWord, b: UWord): Word {
    fsDiskRequest[0] = header
    fsDiskRequest[1] = fsGeneration
    fsDiskRequest[2] = a
    fsDiskRequest[3] = b
    return fsDiskCall(disk, 16)
}

// The sector (relative to the volume) into dest[0..128).
let fsLoadSector(disk: UWord, sector: UWord, dest: *mut UWord): Word {
    fsStageSector = NO_SECTOR
    let loaded: Word = fsDisk(disk, DISK_LOAD_HEADER, sector * SECTOR_SIZE, 0)
    if loaded != 0 return loaded
    if fsDiskResponse[3] != SECTOR_SIZE return -ERRNO_EPROTO
    fsStageSector = sector
    for chunk: UWord in 0..32 {
        let peeked: Word = fsDisk(disk, DISK_PEEK_HEADER, chunk * 16, 0)
        if peeked != 0 return peeked
        if fsDiskResponse[3] != 16 return -ERRNO_EPROTO
        for k: UWord in 0..4 dest[chunk * 4 + k] = fsDiskResponse[4 + k]
    }
    return 0
}

let fsStoreSector(disk: UWord, sector: UWord, source: *UWord): Word {
    fsStageSector = NO_SECTOR
    for chunk: UWord in 0..32 {
        fsDiskRequest[0] = DISK_POKE_HEADER
        fsDiskRequest[1] = fsGeneration
        fsDiskRequest[2] = chunk * 16
        fsDiskRequest[3] = 0
        for k: UWord in 0..4 fsDiskRequest[4 + k] = source[chunk * 4 + k]
        let poked: Word = fsDiskCall(disk, 32)
        if poked != 0 return poked
    }
    let stored: Word = fsDisk(disk, DISK_STORE_HEADER, sector * SECTOR_SIZE, 0)
    if stored == 0 fsStageSector = sector
    return stored
}

let fsSyncDisk(disk: UWord): Word {
    return fsDisk(disk, DISK_SYNC_HEADER, 0, 0)
}

// ---- Metadata -----------------------------------------------------------

let fsUsed(table: *UWord, slot: UWord): Bool {
    return table[HEADER_WORDS + slot * ENTRY_WORDS] & 255 != 0
}

// Little-endian byte access to a word array. Messages and the sector buffer are
// word arrays on both sides of every copy, so one width is used throughout.
let fsByteGet(words: *UWord, index: UWord): UWord {
    return (words[index >> 2] >> ((index & 3) << 3)) & 255
}
let fsBytePut(words: *mut UWord, index: UWord, value: UWord): Void {
    let shift: UWord = (index & 3) << 3
    words[index >> 2] = (words[index >> 2] & ~(255 << shift)) | (value << shift)
}

// Names are 1..16 printable bytes, NUL padded, no byte after the first NUL.
let fsNameValid(name: *UWord): Bool {
    let mut ended: Bool = false
    for i: UWord in 0..16 {
        let c: UWord = fsByteGet(name, i)
        if ended {
            if c != 0 return false
        } else if c == 0 {
            if i == 0 return false
            ended = true
        } else if c < 0x21 || c > 0x7E return false
    }
    return true
}

let fsFind(name: *UWord): UWord {
    for slot: UWord in 0..FS_MAX_FILES {
        let base: UWord = HEADER_WORDS + slot * ENTRY_WORDS
        if fsTable[base] & 255 != 0 && fsTable[base] == name[0] && fsTable[base + 1] == name[1] &&
            fsTable[base + 2] == name[2] && fsTable[base + 3] == name[3] return slot
    }
    return SLOT_NONE
}

// Slot of a live id (serial << 5 | slot), or SLOT_NONE for a stale or bad one.
let fsLookup(id: UWord): UWord {
    let slot: UWord = id & 31
    let serial: UWord = id >> 5
    if slot >= FS_MAX_FILES || serial == 0 return SLOT_NONE
    let base: UWord = HEADER_WORDS + slot * ENTRY_WORDS
    if !fsUsed(&fsTable[0], slot) || fsTable[base + E_SERIAL] != serial return SLOT_NONE
    return slot
}

// A copy read from the medium: framing, checksum, and every invariant that the
// allocator relies on. A copy that fails any of them is as good as absent.
let fsCopyValid(table: *UWord, sectors: UWord): Bool {
    if table[H_MAGIC] != FS_MAGIC || table[H_DATA] != FS_DATA_START || table[H_SECTORS] != sectors ||
        table[H_RESERVED] != 0 || table[H_RESERVED + 1] != 0 || table[H_SERIAL] == 0 ||
        table[H_SERIAL] > MAX_SERIAL + 1 || table[H_GENERATION] == 0 return false
    if table[H_CRC] != fsCrc(table) return false
    for slot: UWord in 0..FS_MAX_FILES {
        let base: UWord = HEADER_WORDS + slot * ENTRY_WORDS
        if table[base] & 255 == 0 {
            for w: UWord in 0..ENTRY_WORDS {
                if table[base + w] != 0 return false
            }
        } else {
            if !fsNameValid(&table[base]) return false
            let first: UWord = table[base + E_FIRST]
            let capacity: UWord = table[base + E_CAPACITY]
            if capacity == 0 || first < FS_DATA_START || first > sectors || capacity > sectors - first return false
            if table[base + E_SIZE] > capacity * SECTOR_SIZE return false
            if table[base + E_SERIAL] == 0 || table[base + E_SERIAL] >= table[H_SERIAL] return false
            for other: UWord in 0..slot {
                let against: UWord = HEADER_WORDS + other * ENTRY_WORDS
                if table[against] & 255 != 0 {
                    if table[against] == table[base] && table[against + 1] == table[base + 1] &&
                        table[against + 2] == table[base + 2] && table[against + 3] == table[base + 3] return false
                    if first < table[against + E_FIRST] + table[against + E_CAPACITY] &&
                        table[against + E_FIRST] < first + capacity return false
                }
            }
        }
    }
    return true
}

let fsReadCopy(disk: UWord, copy: UWord, dest: *mut UWord): Word {
    let first: Word = fsLoadSector(disk, copy * 2, &mut dest[0])
    if first != 0 return first
    return fsLoadSector(disk, copy * 2 + 1, &mut dest[SECTOR_WORDS])
}

let fsCopyTable(destination: *mut UWord, source: *UWord): Void {
    for w: UWord in 0..TABLE_WORDS destination[w] = source[w]
}

// Adopts the valid copy with the highest generation. A medium that holds no
// valid copy is not mounted: every request then answers -ENODEV.
let fsMount(disk: UWord): Word {
    let stat: Word = fsDisk(disk, DISK_STAT_HEADER, 0, 0)
    if stat != 0 return stat
    let bytes: UWord = fsDiskResponse[3]
    if bytes & 511 != 0 || bytes / SECTOR_SIZE < FS_MIN_SECTORS return -ERRNO_ENODEV
    let sectors: UWord = bytes / SECTOR_SIZE
    let flags: Word = fsDisk(disk, DISK_FLAGS_HEADER, 0, 0)
    if flags != 0 return flags
    fsWritable = fsDiskResponse[3] & EXTENT_WRITE != 0
    let readA: Word = fsReadCopy(disk, 0, &mut fsCommitted[0])
    if readA != 0 return readA
    let readB: Word = fsReadCopy(disk, 1, &mut fsScratch[0])
    if readB != 0 return readB
    let okA: Bool = fsCopyValid(&fsCommitted[0], sectors)
    let okB: Bool = fsCopyValid(&fsScratch[0], sectors)
    if okB && (!okA || fsScratch[H_GENERATION] > fsCommitted[H_GENERATION]) {
        fsCopyTable(&mut fsCommitted[0], &fsScratch[0])
        fsActive = 1
    } else if okA fsActive = 0
    else return -ERRNO_ENODEV
    fsCopyTable(&mut fsTable[0], &fsCommitted[0])
    fsBufferSector = NO_SECTOR
    fsStageSector = NO_SECTOR
    fsBufferDirty = false
    fsDataDirty = false
    fsMetaDirty = false
    fsMounted = true
    return 0
}

// ---- Space --------------------------------------------------------------

// True when [start, start + need) touches an extent of the table.
let fsOverlaps(table: *UWord, start: UWord, need: UWord): Bool {
    for slot: UWord in 0..FS_MAX_FILES {
        let base: UWord = HEADER_WORDS + slot * ENTRY_WORDS
        if table[base] & 255 != 0 {
            if start < table[base + E_FIRST] + table[base + E_CAPACITY] && table[base + E_FIRST] < start + need return true
        }
    }
    return false
}

// Free means free in the working table and in the committed one: an extent the
// last commit still refers to is not reused until the next commit replaces it.
let fsFits(start: UWord, need: UWord): Bool {
    if need > fsTable[H_SECTORS] || start > fsTable[H_SECTORS] - need return false
    return !fsOverlaps(&fsTable[0], start, need) && !fsOverlaps(&fsCommitted[0], start, need)
}

// First fit: the lowest sector with `need` free sectors after it, or NO_SECTOR.
let fsAllocate(need: UWord): UWord {
    if need == 0 return NO_SECTOR
    let mut best: UWord = NO_SECTOR
    if fsFits(FS_DATA_START, need) best = FS_DATA_START
    for t: UWord in 0..2 {
        let mut table: *UWord = &fsTable[0]
        if t == 1 table = &fsCommitted[0]
        for slot: UWord in 0..FS_MAX_FILES {
            let base: UWord = HEADER_WORDS + slot * ENTRY_WORDS
            if table[base] & 255 != 0 {
                let end: UWord = table[base + E_FIRST] + table[base + E_CAPACITY]
                if end < best && fsFits(end, need) best = end
            }
        }
    }
    return best
}

// Sectors that are free under the same rule as fsAllocate.
let fsFree(): UWord {
    let total: UWord = fsTable[H_SECTORS]
    let mut free: UWord = 0
    let mut cursor: UWord = FS_DATA_START
    while cursor < total {
        let mut moved: Bool = true
        while moved {
            moved = false
            for t: UWord in 0..2 {
                let mut table: *UWord = &fsTable[0]
                if t == 1 table = &fsCommitted[0]
                for slot: UWord in 0..FS_MAX_FILES {
                    let base: UWord = HEADER_WORDS + slot * ENTRY_WORDS
                    if table[base] & 255 != 0 && table[base + E_FIRST] <= cursor &&
                        table[base + E_FIRST] + table[base + E_CAPACITY] > cursor {
                        cursor = table[base + E_FIRST] + table[base + E_CAPACITY]
                        moved = true
                    }
                }
            }
        }
        if cursor < total {
            let mut next: UWord = total
            for t: UWord in 0..2 {
                let mut table: *UWord = &fsTable[0]
                if t == 1 table = &fsCommitted[0]
                for slot: UWord in 0..FS_MAX_FILES {
                    let base: UWord = HEADER_WORDS + slot * ENTRY_WORDS
                    if table[base] & 255 != 0 && table[base + E_FIRST] > cursor && table[base + E_FIRST] < next
                        next = table[base + E_FIRST]
                }
            }
            free += next - cursor
            cursor = next
        }
    }
    return free
}

// ---- Sector buffer and commit ------------------------------------------

let fsFlushBuffer(disk: UWord): Word {
    if !fsBufferDirty return 0
    let stored: Word = fsStoreSector(disk, fsBufferSector, &fsBuffer[0])
    if stored != 0 return stored
    fsBufferDirty = false
    fsDataDirty = true
    return 0
}

// Makes `sector` the buffered one. With load false the caller knows that nothing
// in the sector is valid yet (it lies beyond the end of a file), so it starts zeroed.
let fsSelect(disk: UWord, sector: UWord, load: Bool): Word {
    if fsBufferSector == sector return 0
    let flushed: Word = fsFlushBuffer(disk)
    if flushed != 0 return flushed
    fsBufferSector = NO_SECTOR
    if load {
        let loaded: Word = fsLoadSector(disk, sector, &mut fsBuffer[0])
        if loaded != 0 return loaded
    } else {
        for i: UWord in 0..SECTOR_WORDS fsBuffer[i] = 0
    }
    fsBufferSector = sector
    return 0
}

// Forgets the buffered sector when it lies in a file that is going away.
let fsDropBuffer(first: UWord, capacity: UWord): Void {
    if fsBufferSector != NO_SECTOR && fsBufferSector >= first && fsBufferSector - first < capacity {
        fsBufferSector = NO_SECTOR
        fsBufferDirty = false
    }
}

// Makes everything done so far durable: data first, then the metadata copy that
// is not current. A failure leaves the previous commit current and the work pending.
let fsCommit(disk: UWord): Word {
    let flushed: Word = fsFlushBuffer(disk)
    if flushed != 0 return flushed
    if fsDataDirty {
        let synced: Word = fsSyncDisk(disk)
        if synced != 0 return synced
        fsDataDirty = false
    }
    if !fsMetaDirty return 0
    fsTable[H_GENERATION] = fsCommitted[H_GENERATION] + 1
    fsTable[H_CRC] = fsCrc(&fsTable[0])
    let target: UWord = 1 - fsActive
    let first: Word = fsStoreSector(disk, target * 2, &fsTable[0])
    if first != 0 return first
    let second: Word = fsStoreSector(disk, target * 2 + 1, &fsTable[SECTOR_WORDS])
    if second != 0 return second
    let synced: Word = fsSyncDisk(disk)
    if synced != 0 return synced
    fsCopyTable(&mut fsCommitted[0], &fsTable[0])
    fsActive = target
    fsMetaDirty = false
    return 0
}

// ---- Requests -----------------------------------------------------------

let fsFail(response: *mut UWord, status: Word): Void {
    response[1] = status as UWord
}

let fsEntryInfo(response: *mut UWord, slot: UWord): Void {
    let base: UWord = HEADER_WORDS + slot * ENTRY_WORDS
    response[1] = 0
    response[3] = (fsTable[base + E_SERIAL] << 5) | slot
    response[4] = fsTable[base + E_SIZE]
    response[5] = fsTable[base + E_CAPACITY] * SECTOR_SIZE
}

let fsOpen(request: *UWord, response: *mut UWord, disk: UWord): Void {
    let flags: UWord = request[2]
    let capacity: UWord = request[3]
    let name: *UWord = &request[4]
    if flags & ~(FS_CREATE | FS_EXCL | FS_TRUNC) != 0 || !fsNameValid(name) return
    if flags & FS_EXCL != 0 && flags & FS_CREATE == 0 return
    let slot: UWord = fsFind(name)
    if slot != SLOT_NONE {
        if flags & FS_EXCL != 0 {
            fsFail(response, -ERRNO_EEXIST)
            return
        }
        if flags & FS_TRUNC != 0 {
            // Replacing a file takes a fresh extent and a fresh id, so the old
            // contents stay intact until the commit that swaps them.
            if !fsWritable {
                fsFail(response, -ERRNO_EROFS)
                return
            }
            let base: UWord = HEADER_WORDS + slot * ENTRY_WORDS
            let mut wanted: UWord = fsTable[base + E_CAPACITY]
            if capacity != 0 wanted = capacity
            if fsTable[H_SERIAL] > MAX_SERIAL {
                fsFail(response, -ERRNO_ENOSPC)
                return
            }
            let first: UWord = fsAllocate(wanted)
            if first == NO_SECTOR {
                fsFail(response, -ERRNO_ENOSPC)
                return
            }
            fsDropBuffer(fsTable[base + E_FIRST], fsTable[base + E_CAPACITY])
            fsTable[base + E_FIRST] = first
            fsTable[base + E_CAPACITY] = wanted
            fsTable[base + E_SIZE] = 0
            fsTable[base + E_SERIAL] = fsTable[H_SERIAL]
            fsTable[H_SERIAL] += 1
            fsMetaDirty = true
        }
        fsEntryInfo(response, slot)
        return
    }
    if flags & FS_CREATE == 0 {
        fsFail(response, -ERRNO_ENOENT)
        return
    }
    if !fsWritable {
        fsFail(response, -ERRNO_EROFS)
        return
    }
    if capacity == 0 return
    let mut free: UWord = SLOT_NONE
    for candidate: UWord in 0..FS_MAX_FILES {
        if free == SLOT_NONE && !fsUsed(&fsTable[0], candidate) free = candidate
    }
    if free == SLOT_NONE || fsTable[H_SERIAL] > MAX_SERIAL {
        fsFail(response, -ERRNO_ENOSPC)
        return
    }
    let first: UWord = fsAllocate(capacity)
    if first == NO_SECTOR {
        fsFail(response, -ERRNO_ENOSPC)
        return
    }
    let base: UWord = HEADER_WORDS + free * ENTRY_WORDS
    for w: UWord in 0..4 fsTable[base + w] = name[w]
    fsTable[base + E_SIZE] = 0
    fsTable[base + E_FIRST] = first
    fsTable[base + E_CAPACITY] = capacity
    fsTable[base + E_SERIAL] = fsTable[H_SERIAL]
    fsTable[H_SERIAL] += 1
    fsMetaDirty = true
    fsEntryInfo(response, free)
}

let fsStat(request: *UWord, response: *mut UWord): Void {
    if request[3] != 0 return
    let slot: UWord = fsLookup(request[2])
    if slot == SLOT_NONE {
        fsFail(response, -ERRNO_ENOENT)
        return
    }
    response[1] = 0
    response[3] = fsTable[HEADER_WORDS + slot * ENTRY_WORDS + E_SIZE]
    response[4] = fsTable[HEADER_WORDS + slot * ENTRY_WORDS + E_CAPACITY] * SECTOR_SIZE
}

// Bytes [inSector, inSector + n) of a sector into the response data words, from
// the freshest copy: the buffer when it holds the sector, else the Disk stage.
let fsReadSector(disk: UWord, sector: UWord, inSector: UWord, n: UWord, response: *mut UWord): Word {
    if fsBufferSector == sector {
        for i: UWord in 0..n fsBytePut(&mut response[4], i, fsByteGet(&fsBuffer[0], inSector + i))
        return 0
    }
    if fsStageSector != sector {
        fsStageSector = NO_SECTOR
        let loaded: Word = fsDisk(disk, DISK_LOAD_HEADER, sector * SECTOR_SIZE, 0)
        if loaded != 0 return loaded
        if fsDiskResponse[3] != SECTOR_SIZE return -ERRNO_EPROTO
        fsStageSector = sector
    }
    let mut chunk: UWord = inSector >> 4
    let last: UWord = (inSector + n - 1) >> 4
    while chunk <= last {
        let peeked: Word = fsDisk(disk, DISK_PEEK_HEADER, chunk * 16, 0)
        if peeked != 0 return peeked
        if fsDiskResponse[3] != 16 return -ERRNO_EPROTO
        for i: UWord in 0..16 {
            let position: UWord = chunk * 16 + i
            if position >= inSector && position < inSector + n
                fsBytePut(&mut response[4], position - inSector, fsByteGet(&fsDiskResponse[4], i))
        }
        chunk += 1
    }
    return 0
}

let fsRead(request: *UWord, response: *mut UWord, disk: UWord): Void {
    let count: UWord = request[4]
    if count == 0 || count > 16 return
    let slot: UWord = fsLookup(request[2])
    if slot == SLOT_NONE {
        fsFail(response, -ERRNO_ENOENT)
        return
    }
    let base: UWord = HEADER_WORDS + slot * ENTRY_WORDS
    let size: UWord = fsTable[base + E_SIZE]
    let offset: UWord = request[3]
    if offset > size return
    if offset == size {
        response[1] = 0
        return
    }
    let mut n: UWord = count
    if n > size - offset n = size - offset
    let inSector: UWord = offset & 511
    if n > SECTOR_SIZE - inSector n = SECTOR_SIZE - inSector
    let served: Word = fsReadSector(disk, fsTable[base + E_FIRST] + offset / SECTOR_SIZE, inSector, n, response)
    if served != 0 {
        fsFail(response, served)
        for i: UWord in 3..8 response[i] = 0
        return
    }
    response[1] = 0
    response[3] = n
}

// A write of 1..16 bytes that stays inside one sector and the file's capacity.
// The reply says how many bytes were taken; the client sends the rest again.
let fsWrite(request: *UWord, size: UWord, response: *mut UWord, disk: UWord): Void {
    let want: UWord = size - 16
    if !fsWritable {
        fsFail(response, -ERRNO_EROFS)
        return
    }
    let slot: UWord = fsLookup(request[2])
    if slot == SLOT_NONE {
        fsFail(response, -ERRNO_ENOENT)
        return
    }
    let base: UWord = HEADER_WORDS + slot * ENTRY_WORDS
    let fileSize: UWord = fsTable[base + E_SIZE]
    let limit: UWord = fsTable[base + E_CAPACITY] * SECTOR_SIZE
    let offset: UWord = request[3]
    if offset > fileSize return
    if offset >= limit {
        fsFail(response, -ERRNO_ENOSPC)
        return
    }
    let mut n: UWord = want
    if n > limit - offset n = limit - offset
    let inSector: UWord = offset & 511
    if n > SECTOR_SIZE - inSector n = SECTOR_SIZE - inSector
    // A sector that starts at or beyond the end of the file holds nothing valid.
    let selected: Word = fsSelect(disk, fsTable[base + E_FIRST] + offset / SECTOR_SIZE, (offset - inSector) < fileSize)
    if selected != 0 {
        fsFail(response, selected)
        return
    }
    for i: UWord in 0..n fsBytePut(&mut fsBuffer[0], inSector + i, fsByteGet(&request[4], i))
    fsBufferDirty = true
    if offset + n > fileSize {
        fsTable[base + E_SIZE] = offset + n
        fsMetaDirty = true
    }
    response[1] = 0
    response[3] = n
}

let fsDelete(request: *UWord, response: *mut UWord): Void {
    let name: *UWord = &request[4]
    if request[2] != 0 || request[3] != 0 || !fsNameValid(name) return
    if !fsWritable {
        fsFail(response, -ERRNO_EROFS)
        return
    }
    let slot: UWord = fsFind(name)
    if slot == SLOT_NONE {
        fsFail(response, -ERRNO_ENOENT)
        return
    }
    let base: UWord = HEADER_WORDS + slot * ENTRY_WORDS
    fsDropBuffer(fsTable[base + E_FIRST], fsTable[base + E_CAPACITY])
    for w: UWord in 0..ENTRY_WORDS fsTable[base + w] = 0
    fsMetaDirty = true
    response[1] = 0
}

// The index-th file in slot order: size in word 3, name in the data words.
let fsList(request: *UWord, response: *mut UWord): Void {
    if request[3] != 0 return
    let mut seen: UWord = 0
    let mut found: UWord = SLOT_NONE
    for slot: UWord in 0..FS_MAX_FILES {
        if fsUsed(&fsTable[0], slot) {
            if seen == request[2] && found == SLOT_NONE found = slot
            seen += 1
        }
    }
    if found == SLOT_NONE {
        fsFail(response, -ERRNO_ENOENT)
        return
    }
    let base: UWord = HEADER_WORDS + found * ENTRY_WORDS
    response[1] = 0
    response[3] = fsTable[base + E_SIZE]
    for w: UWord in 0..4 response[4 + w] = fsTable[base + w]
}

// Data sectors, free sectors, the committed generation and a flag word:
// bit 0 writable, bit 1 metadata not yet committed, bit 2 data not yet flushed.
let fsInfo(request: *UWord, response: *mut UWord): Void {
    if request[2] != 0 || request[3] != 0 return
    let mut flags: UWord = 0
    if fsWritable flags |= 1
    if fsMetaDirty flags |= 2
    if fsBufferDirty || fsDataDirty flags |= 4
    response[1] = 0
    response[3] = fsTable[H_SECTORS] - FS_DATA_START
    response[4] = fsFree()
    response[5] = fsCommitted[H_GENERATION]
    response[6] = flags
}

let fsHandle(request: *UWord, size: UWord, response: *mut UWord, disk: UWord): Void {
    for i: UWord in 0..8 response[i] = 0
    response[0] = FS_RESPONSE_HEADER
    response[1] = (-ERRNO_EINVAL) as UWord
    response[2] = fsGeneration
    let header: UWord = request[0]
    let isOpen: Bool = header == FS_OPEN_HEADER && size == 32
    let isStat: Bool = header == FS_STAT_HEADER && size == 16
    let isRead: Bool = header == FS_READ_HEADER && size == 20
    let isWrite: Bool = header == FS_WRITE_HEADER && size > 16 && size <= 32
    let isSync: Bool = header == FS_SYNC_HEADER && size == 16
    let isDelete: Bool = header == FS_DELETE_HEADER && size == 32
    let isList: Bool = header == FS_LIST_HEADER && size == 16
    let isInfo: Bool = header == FS_INFO_HEADER && size == 16
    if !isOpen && !isStat && !isRead && !isWrite && !isSync && !isDelete && !isList && !isInfo return
    if request[1] != fsGeneration {
        fsFail(response, -ERRNO_EPIPE)
        return
    }
    if !fsMounted {
        let mounted: Word = fsMount(disk)
        if mounted != 0 {
            fsFail(response, mounted)
            return
        }
    }
    if isOpen fsOpen(request, response, disk)
    else if isStat fsStat(request, response)
    else if isRead fsRead(request, response, disk)
    else if isWrite fsWrite(request, size, response, disk)
    else if isDelete fsDelete(request, response)
    else if isList fsList(request, response)
    else if isInfo fsInfo(request, response)
    else if request[2] == 0 && request[3] == 0 {
        // A device failure ends the service (fsFailed); its replacement mounts
        // the committed state.
        let committed: Word = fsCommit(disk)
        fsFail(response, committed)
    }
}

let fsMain(start: *ServiceStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != SERVICE_START_BYTES ||
        !serviceStartValid(start) || start.role != START_ROLE_FILE) exit(1)
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(start.endpoint, &mut fsRequest[0] as *mut UByte, 32, &mut accepted)
        if size < 0 exit(1)
        fsHandle(&fsRequest[0], size as UWord, &mut fsResponse[0], start.bitmapEndpoint)
        let sent: Word = reply(accepted.replyToken, &fsResponse[0] as *UByte, 32)
        if sent > 32 exit(1)
        if fsFailed exit(1)
    }
}
let fsSetGeneration(generation: UWord): Void {
    fsGeneration = generation
    fsTimed = true
}
let fsDependencyFailed(): Bool { return fsFailed }
export { fsDependencyFailed, fsSetGeneration, fsHandle, fsMount, fsCrc, fsMain }

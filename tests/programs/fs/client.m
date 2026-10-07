// Filesystem acceptance client (G7). It drives the WFS1 service over real IPC, the
// Disk service and the DMA/IRQ path, and names a failing step with its exit code.
// The volume is produced by tools/wfs.py (tests/probe_fs_cpu.py reads it back
// afterwards and checks what actually reached the medium).
import { ServiceStart, serviceStartValid } from "../../../src/task/service_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_CLIENT, START_PROTOCOL_FILE,
    FS_CREATE, FS_EXCL, FS_TRUNC, ERRNO_EINVAL, ERRNO_ENOENT, ERRNO_EEXIST, ERRNO_ENOSPC } from "../../../src/arch/wrm081632/defs.m"
import { FsFile, FsInfo, fsOpen, fsStat, fsReadAt, fsWriteAt, fsSync, fsRemove, fsListAt, fsInfo,
    fsReadAll, fsWriteAll, fsReadFile, fsWriteFile } from "../../../user/services/fsclient.m"
import { exit } from "../../../user/syscalls.m"

let mut fcBuffer: UByte[2048]
let mut fcExpect: UByte[2048]
let mut fcName: UByte[16]
let mut fcSize: UWord[1]
let mut fcFile: FsFile
let mut fcOther: FsFile
let mut fcInfo: FsInfo

// The same byte sequence as tests/probe_fs_cpu.py: (i * 7 + 3 + tag) & 255.
let fcPattern(tag: UWord, bytes: UWord): Void {
    for i: UWord in 0..bytes fcExpect[i] = ((i * 7 + 3 + tag) & 255) as UByte
}

let fcMatches(bytes: UWord): Bool {
    for i: UWord in 0..bytes {
        if fcBuffer[i] != fcExpect[i] return false
    }
    return true
}

let fcMain(fs: UWord): Word {
    // 1. A writable mount with nothing pending, on a volume that has a file already.
    if fsInfo(fs, &mut fcInfo) != 0 return 2
    if fcInfo.flags != 1 || fcInfo.freeSectors == 0 return 3
    let motd: Word = fsReadFile(fs, "motd", &mut fcBuffer[0], 2048)
    if motd != 17 return 4
    if fcBuffer[0] != 'W' as UByte || fcBuffer[16] != 10 return 5

    // 2. Create, write across sector boundaries, read it back before any sync.
    if fsOpen(fs, "notes", FS_CREATE | FS_EXCL, 4, &mut fcFile) != 0 return 10
    if fcFile.size != 0 || fcFile.capacity != 2048 return 11
    fcPattern(1, 1500)
    if fsWriteAll(fs, fcFile.id, 0, &fcExpect[0], 1500) != 0 return 12
    if fsReadAll(fs, fcFile.id, &mut fcBuffer[0], 2048) != 1500 return 13
    if !fcMatches(1500) return 14
    if fsStat(fs, fcFile.id, &mut fcOther) != 0 || fcOther.size != 1500 return 15
    if fsInfo(fs, &mut fcInfo) != 0 || fcInfo.flags & 6 == 0 return 16

    // 3. Make it durable; nothing is pending afterwards.
    if fsSync(fs) != 0 return 20
    if fsInfo(fs, &mut fcInfo) != 0 || fcInfo.flags != 1 return 21

    // 4. Refusals leave the file alone.
    if fsOpen(fs, "notes", FS_CREATE | FS_EXCL, 1, &mut fcOther) != -ERRNO_EEXIST return 30
    if fsOpen(fs, "absent", 0, 0, &mut fcOther) != -ERRNO_ENOENT return 31
    if fsWriteAt(fs, fcFile.id, 2048, &fcExpect[0], 1) != -ERRNO_EINVAL return 32
    if fsWriteAt(fs, fcFile.id, 1501, &fcExpect[0], 1) >= 0 return 33
    if fsReadAll(fs, fcFile.id, &mut fcBuffer[0], 2048) != 1500 return 34
    if !fcMatches(1500) return 35
    // A file never grows past the capacity it was created with.
    if fsOpen(fs, "cap", FS_CREATE, 1, &mut fcOther) != 0 return 36
    fcPattern(0, 512)
    if fsWriteAll(fs, fcOther.id, 0, &fcExpect[0], 512) != 0 return 37
    if fsWriteAt(fs, fcOther.id, 512, &fcExpect[0], 1) != -ERRNO_ENOSPC return 38
    if fsRemove(fs, "cap") != 0 return 39

    // 5. Replacing a file: the old id goes stale, the new contents are exact.
    if fsOpen(fs, "notes", FS_TRUNC, 2, &mut fcOther) != 0 return 40
    if fcOther.id == fcFile.id || fcOther.size != 0 || fcOther.capacity != 1024 return 41
    if fsReadAt(fs, fcFile.id, 0, &mut fcBuffer[0], 16) != -ERRNO_ENOENT return 42
    fcPattern(2, 700)
    if fsWriteAll(fs, fcOther.id, 0, &fcExpect[0], 700) != 0 return 43
    if fsSync(fs) != 0 return 44
    if fsReadAll(fs, fcOther.id, &mut fcBuffer[0], 2048) != 700 return 45
    if !fcMatches(700) return 46

    // 6. Delete and list.
    if fsRemove(fs, "motd") != 0 return 50
    if fsRemove(fs, "motd") != -ERRNO_ENOENT return 51
    if fsSync(fs) != 0 return 52
    if fsListAt(fs, 0, &mut fcName[0], &mut fcSize[0]) != 0 return 53
    if fcName[0] != 'n' as UByte || fcName[5] != 0 || fcSize[0] != 700 return 54
    if fsListAt(fs, 1, &mut fcName[0], &mut fcSize[0]) != -ERRNO_ENOENT return 55

    // 7. Space comes back only after a commit: fill the volume, delete one file,
    // and the freed sectors are still refused until the commit that removes it.
    let mut made: UWord = 0
    fcName[0] = 'f' as UByte
    fcName[2] = 0
    for i: UWord in 0..9 {
        fcName[1] = ('0' as UWord + i) as UByte
        let opened: Word = fsOpen(fs, &fcName[0], FS_CREATE, 4, &mut fcOther)
        if opened == 0 made += 1
        else if opened != -ERRNO_ENOSPC return 60
    }
    if made < 2 || made == 9 return 61
    if fsInfo(fs, &mut fcInfo) != 0 || fcInfo.freeSectors >= 4 return 62
    if fsSync(fs) != 0 return 68
    fcName[1] = '0'
    if fsRemove(fs, &fcName[0]) != 0 return 63
    if fsOpen(fs, "again", FS_CREATE, 4, &mut fcOther) != -ERRNO_ENOSPC return 64
    if fsSync(fs) != 0 return 65
    if fsOpen(fs, "again", FS_CREATE, 4, &mut fcOther) != 0 return 66
    for i: UWord in 1..9 {
        fcName[1] = ('0' as UWord + i) as UByte
        let removed: Word = fsRemove(fs, &fcName[0])
        if removed != 0 && removed != -ERRNO_ENOENT return 67
    }

    // 8. Leave a known final state for the probe to check on the medium.
    if fsRemove(fs, "again") != 0 return 70
    fcPattern(3, 40)
    if fsWriteFile(fs, "last", &fcExpect[0], 40) != 0 return 71
    if fsReadFile(fs, "last", &mut fcBuffer[0], 2048) != 40 || !fcMatches(40) return 72
    return 0
}

let fsClientMain(start: *ServiceStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != SERVICE_START_BYTES ||
        !serviceStartValid(start) || start.role != START_ROLE_CLIENT || start.protocol != START_PROTOCOL_FILE) exit(1)
    exit(fcMain(start.endpoint))
}
export { fsClientMain }

// Files-backed image loading (G1). Reads one Files file into a caller buffer and
// asks the kernel to load it as an unpublished child. The kernel snapshots the
// bytes and applies the image checks and quotas; this module holds no policy.
import { fileSize, fileRead } from "services/client.m"
import { loadTask } from "syscalls.m"
import { ERRNO_EINVAL, ERRNO_EPROTO } from "../src/arch/wrm081632/defs.m"

let LOAD_PIECE: UWord = 16 // the most a Files read returns

// Whole file into buffer[0..size). Returns the size, or a negative errno:
// EINVAL when the file does not fit, EPROTO for a short or inconsistent read.
let readImage(files: UWord, buffer: *mut UByte, capacity: UWord): Word {
    let size: Word = fileSize(files)
    if size < 0 return size
    if size == 0 || size as UWord > capacity return -ERRNO_EINVAL
    let mut offset: UWord = 0
    while offset < size as UWord {
        let mut piece: UWord = size as UWord - offset
        if piece > LOAD_PIECE piece = LOAD_PIECE
        let count: Word = fileRead(files, offset, &mut buffer[offset], piece)
        if count < 0 return count
        if count as UWord != piece return -ERRNO_EPROTO
        offset += piece
    }
    return size
}

// readImage, then the kernel load. Returns the child's reference or an errno.
let loadFileImage(files: UWord, buffer: *mut UByte, capacity: UWord): Word {
    let size: Word = readImage(files, buffer, capacity)
    if size < 0 return size
    return loadTask(buffer, size as UWord)
}
export { readImage, loadFileImage }

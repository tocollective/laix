// Build-issued approved storage root (tools/storage_root.py). The kernel reads
// only the framing below; the bytes it covers have no kernel-visible format.
extern let approvedStorageRoot: UByte

type StorageRoot {
    magic: UWord,
    version: UWord,
    bytes: UWord,
    flags: UWord,
}
let STORAGE_ROOT_MAGIC: UWord = 0x31525357 // 'WSR1', little endian
let STORAGE_ROOT_VERSION: UWord = 1
let STORAGE_ROOT_MAX: UWord = 0x7FFFFFFF
let STORAGE_ROOT_WRITABLE: UWord = 1 // flags bit 0 (write contract, G7)
let STORAGE_ROOT_SECTOR_MASK: UWord = 511

// Returns the approved byte length, or zero for a missing or malformed root.
// Flags bit 0 (writable) is the only defined bit; a writable root must cover
// whole sectors because the kernel never does a partial-sector write.
let approvedStorageBytes(): UWord {
    let root: *StorageRoot = &approvedStorageRoot as UWord as *StorageRoot
    if root.magic != STORAGE_ROOT_MAGIC || root.version != STORAGE_ROOT_VERSION ||
        root.flags & ~STORAGE_ROOT_WRITABLE != 0 || root.bytes == 0 || root.bytes > STORAGE_ROOT_MAX return 0
    if root.flags & STORAGE_ROOT_WRITABLE != 0 && root.bytes & STORAGE_ROOT_SECTOR_MASK != 0 return 0
    return root.bytes
}
// True only for a valid root that carries the writable bit.
let approvedStorageWritable(): Bool {
    let root: *StorageRoot = &approvedStorageRoot as UWord as *StorageRoot
    return approvedStorageBytes() != 0 && root.flags & STORAGE_ROOT_WRITABLE != 0
}
export { StorageRoot, approvedStorageBytes, approvedStorageWritable }

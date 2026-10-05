// Minimal eager heap: one page-backed region per allocation. Metadata stays in
// the program's data segment. No kernel imports, physical addresses or owners.
import { openMemorySpace, allocateRegion, releaseRegion, mapRegion, unmapRegion } from "syscalls.m"
import { PAGE_SIZE, PAGE_MASK, PTE_RW, PTE_U, MEM_REGION_PAGES, MEM_VA_START,
    MEM_RIGHT_ALLOC, MEM_RIGHT_MAP, MEM_RIGHT_UNMAP, ERRNO_EINVAL, ERRNO_ENOMEM } from "../src/arch/wrm081632/defs.m"
let HEAP_REGIONS: UWord = 8
let HEAP_REGION_BYTES: UWord = MEM_REGION_PAGES * PAGE_SIZE
type HeapRegion { token: UWord, pages: UWord }
let mut heapRegions: HeapRegion[HEAP_REGIONS]
let mut heapSpace: UWord
let mut heapError: Word

// Page-aligned pointers are also suitable for all ordinary M value alignments.
// There are eight live blocks of at most 64 KiB, subject to the task budget.
let heapAllocate(bytes: UWord): *mut UByte {
    if bytes == 0 || bytes > HEAP_REGION_BYTES {
        heapError = -ERRNO_EINVAL
        return null
    }
    if heapSpace == 0 {
        let result: Word = openMemorySpace(0, MEM_RIGHT_ALLOC | MEM_RIGHT_MAP | MEM_RIGHT_UNMAP)
        if result < 0 { heapError = result
            return null }
        heapSpace = result as UWord
    }
    for i: UWord in 0..HEAP_REGIONS {
        let region: *mut HeapRegion = &mut heapRegions[i]
        if region.token != 0 continue
        let pages: UWord = (bytes + PAGE_MASK) / PAGE_SIZE
        let result: Word = allocateRegion(heapSpace, pages)
        if result < 0 { heapError = result
            return null }
        let virtual: UWord = MEM_VA_START + i * HEAP_REGION_BYTES
        let mapped: Word = mapRegion(heapSpace, result as UWord, virtual, 0, pages, PTE_RW | PTE_U)
        if mapped != 0 {
            releaseRegion(heapSpace, result as UWord)
            heapError = mapped
            return null
        }
        region.token = result as UWord
        region.pages = pages
        heapError = 0
        return virtual as *mut UByte
    }
    heapError = -ERRNO_ENOMEM
    return null
}

// Only allocation bases may be freed; all complete pages return to the kernel.
// If a future broker pins a region, restore its mapping after denied release.
let heapRelease(pointer: *mut UByte): Word {
    if pointer == null return 0
    let virtual: UWord = pointer as UWord
    if virtual < MEM_VA_START || virtual - MEM_VA_START >= HEAP_REGIONS * HEAP_REGION_BYTES ||
        (virtual - MEM_VA_START) % HEAP_REGION_BYTES != 0 return -ERRNO_EINVAL
    let region: *mut HeapRegion = &mut heapRegions[(virtual - MEM_VA_START) / HEAP_REGION_BYTES]
    if region.token == 0 return -ERRNO_EINVAL
    let unmapped: Word = unmapRegion(heapSpace, virtual, region.pages)
    if unmapped != 0 return unmapped
    let released: Word = releaseRegion(heapSpace, region.token)
    if released != 0 {
        let restored: Word = mapRegion(heapSpace, region.token, virtual, 0, region.pages, PTE_RW | PTE_U)
        if restored != 0 return restored
        return released
    }
    region.token = 0
    region.pages = 0
    return 0
}
export { heapAllocate, heapRelease, heapError }

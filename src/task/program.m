// Only trusted boot embeds can use this limited PT_LOAD constructor. It is
// neither a user ELF loader nor a syscall: no relocations, TLS or dynamic link.
import { taskConstructImage, taskDiscardChecked, taskInitAvailable, taskGet, Task } from "task.m"
import { mapPage, unmapPage } from "../mm/mmu.m"
import { PAGE_NONE, PAGE_USER, allocPage, freePage } from "../mm/memory.m"
import { PAGE_SIZE, PAGE_MASK, PTE_U, PTE_RO, PTE_RW, PTE_RX,
    SERVICE_IMAGE_BASE, SERVICE_IMAGE_END, WORD_BYTES } from "../arch/wrm081632/defs.m"
import { panic } from "../kernel/panic.m"

extern let __start_rodata: UByte
extern let __stop_rodata: UByte
extern let userCodeStart: UByte
extern let userCodeEnd: UByte

type ProgramHeader {
    kind: UWord,
    offset: UWord,
    virtual: UWord,
    physical: UWord,
    fileBytes: UWord,
    memoryBytes: UWord,
    flags: UWord,
    alignment: UWord,
}

let programRollback(id: UWord): UWord {
    if !taskDiscardChecked(id) panic("could not discard service image", null)
    return 0
}

let taskConstructProgram(start: UWord, end: UWord): UWord {
    if start < (&__start_rodata as UWord) || end > (&__stop_rodata as UWord) ||
        end <= start || start % WORD_BYTES != 0 || end - start < 52 return 0
    let bytes: UWord = end - start
    let header: *UWord = start as *UWord
    if header[0] != 0x464C457F || header[1] != 0x00010101 || header[2] != 0 ||
        header[3] != 0 || header[4] != 0x08160002 || header[5] != 1 ||
        header[10] != 0x00200034 return 0
    let phoff: UWord = header[7]
    let count: UWord = header[11] & 0xFFFF
    if count == 0 || count > 3 || phoff % WORD_BYTES != 0 || phoff < 52 ||
        phoff > bytes || count > (bytes - phoff) / sizeof(ProgramHeader) return 0
    let segments: *ProgramHeader = (start + phoff) as *ProgramHeader
    let mut executableEntry: Bool = false
    let mut totalPages: UWord = 0
    for i: UWord in 0..count {
        let segment: *ProgramHeader = &segments[i]
        if segment.kind != 1 || segment.alignment != PAGE_SIZE || segment.memoryBytes == 0 ||
            segment.virtual < SERVICE_IMAGE_BASE || segment.virtual >= SERVICE_IMAGE_END ||
            segment.virtual & PAGE_MASK != 0 || segment.memoryBytes > SERVICE_IMAGE_END - segment.virtual ||
            segment.fileBytes > segment.memoryBytes || segment.offset > bytes ||
            segment.fileBytes > bytes - segment.offset ||
            (segment.flags != 4 && segment.flags != 5 && segment.flags != 6) return 0
        let pages: UWord = (segment.memoryBytes + PAGE_MASK) / PAGE_SIZE
        totalPages += pages
        if totalPages > 64 return 0
        for j: UWord in 0..i {
            let other: *ProgramHeader = &segments[j]
            let otherEnd: UWord = other.virtual + ((other.memoryBytes + PAGE_MASK) & ~PAGE_MASK)
            if segment.virtual < otherEnd && other.virtual < segment.virtual + pages * PAGE_SIZE return 0
        }
        if segment.flags == 5 && header[6] >= segment.virtual &&
            header[6] - segment.virtual < segment.fileBytes && header[6] % WORD_BYTES == 0 executableEntry = true
    }
    if !executableEntry return 0
    let id: UWord = taskConstructImage(&userCodeStart as UWord, &userCodeEnd as UWord, 0)
    if id == 0 return 0
    let task: *mut Task = taskGet(id)
    // Drop the construction fixture before installing the real service code.
    if !unmapPage(task.directory, id, task.userCode) return programRollback(id)
    if !freePage(task.pages[0], id, PAGE_USER) return programRollback(id)
    task.pages[0] = PAGE_NONE
    for i: UWord in 0..count {
        let segment: *ProgramHeader = &segments[i]
        let mut permissions: UWord = PTE_RO | PTE_U
        if segment.flags == 5 permissions = PTE_RX | PTE_U
        if segment.flags == 6 permissions = PTE_RW | PTE_U
        let source: *UByte = (start + segment.offset) as *UByte
        let pages: UWord = (segment.memoryBytes + PAGE_MASK) / PAGE_SIZE
        for page: UWord in 0..pages {
            let physical: UWord = allocPage(id, PAGE_USER)
            if physical == PAGE_NONE return programRollback(id)
            let target: *mut UByte = physical as *mut UByte
            let offset: UWord = page * PAGE_SIZE
            let mut copyBytes: UWord = 0
            if offset < segment.fileBytes {
                copyBytes = segment.fileBytes - offset
                if copyBytes > PAGE_SIZE copyBytes = PAGE_SIZE
            }
            for n: UWord in 0..copyBytes target[n] = source[offset + n]
            if !mapPage(task.directory, id, segment.virtual + offset, physical, permissions) {
                if !freePage(physical, id, PAGE_USER) panic("could not release image page", null)
                return programRollback(id)
            }
        }
    }
    task.userCode = header[6]
    task.context.epc = header[6]
    return id
}

let taskCreateProgram(start: UWord, end: UWord): UWord {
    if !taskInitAvailable() return 0
    return taskConstructProgram(start, end)
}
export { taskCreateProgram, taskConstructProgram }

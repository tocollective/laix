// Device resource descriptors. The rows below are boot data: which role receives
// which MMIO page, VRAM window, read-only blob or IRQ line, and where it appears
// in the receiver's address space. The functions are the mechanism. They know
// hardware classes (what is safe to map, what the kernel keeps), never a device
// by name, so a new row for an existing class needs no change outside this table.
//
// Permissions are not a column. A kind implies them: VRAM is RW|U, MMIO and
// blobs are RO|U, none is executable. A row can never ask for a writable
// register page, and any row that breaks a kernel invariant is ignored.
import { PAGE_SIZE, PAGE_MASK, PTE_RW, PTE_RO, PTE_U, VRAM_BASE, IO_BASE,
    TIMER_IRQ, PIC_LINE_COUNT, VIDEO_BASE, VIDEO_IRQ, DISK0_BASE, DISK1_BASE,
    FLOPPY_BASE, KEYBOARD_IRQ, ETH_IRQ, TIMER_BASE, UART_BASE, POWER_BASE, RNG_BASE,
    SCREEN_VRAM_VA, SCREEN_VIDEO_VA, SCREEN_FONT_VA, SCREEN_VRAM_BYTES } from "../arch/wrm081632/defs.m"
import { fontData, fontDataEnd } from "../console/font/data.m"

let DEVICE_ROLE_SCREEN: UWord = 1
let DEVICE_ROLE_INPUT: UWord = 2
let DEVICE_ROLE_STORAGE: UWord = 3
let DEVICE_ROLE_NET: UWord = 4

let DEVICE_KIND_VRAM: UWord = 1 // exclusive bounded VRAM alias, RW|U
let DEVICE_KIND_MMIO: UWord = 2 // one read-safe register page, RO|U
let DEVICE_KIND_BLOB: UWord = 3 // kernel-embedded read-only blob, RO|U; physical = blob index
let DEVICE_KIND_IRQ: UWord = 4 // interrupt line; no mapping
let DEVICE_KIND_DISK: UWord = 5 // disk register page the broker drives, plus its IRQ line

let DEVICE_ROW_WORDS: UWord = 6 // kind, role, virtual, physical, bytes, line
let DEVICE_ROW_COUNT: UWord = 9
let DEVICE_NONE: UWord = 32 // no row / no line; an impossible PIC line

let deviceRows: UWord[54] = [
    DEVICE_KIND_VRAM, DEVICE_ROLE_SCREEN, SCREEN_VRAM_VA, VRAM_BASE, SCREEN_VRAM_BYTES, 0,
    DEVICE_KIND_MMIO, DEVICE_ROLE_SCREEN, SCREEN_VIDEO_VA, VIDEO_BASE, PAGE_SIZE, 0,
    DEVICE_KIND_BLOB, DEVICE_ROLE_SCREEN, SCREEN_FONT_VA, 0, 0, 0,
    DEVICE_KIND_IRQ, DEVICE_ROLE_SCREEN, 0, 0, 0, VIDEO_IRQ,
    DEVICE_KIND_IRQ, DEVICE_ROLE_INPUT, 0, 0, 0, KEYBOARD_IRQ,
    DEVICE_KIND_DISK, DEVICE_ROLE_STORAGE, 0, DISK0_BASE, 0, 3,
    DEVICE_KIND_DISK, DEVICE_ROLE_STORAGE, 0, DISK1_BASE, 0, 4,
    DEVICE_KIND_DISK, DEVICE_ROLE_STORAGE, 0, FLOPPY_BASE, 0, 6,
    DEVICE_KIND_IRQ, DEVICE_ROLE_NET, 0, 0, 0, ETH_IRQ,
]

// Register policy for the single display owner: offset, bits the owner may
// write, flags, frame check. Anything not listed is not writable.
let REGISTER_IDLE: UWord = 1 // engine must not be busy
let CHECK_NONE: UWord = 0
let CHECK_ENABLE: UWord = 1 // scanout enable must fit the frame in VRAM
let CHECK_MODE: UWord = 2 // value is a new MODE; the frame must fit
let CHECK_START: UWord = 3 // value is a new START; the frame must fit
let REGISTER_RULE_WORDS: UWord = 4
let REGISTER_RULE_COUNT: UWord = 7
let registerRules: UWord[28] = [
    0, 0x0000000A, 0, CHECK_NONE, // STATUS: acknowledge DONE/VBLANK only
    4, 0x00000005, REGISTER_IDLE, CHECK_ENABLE, // CONTROL: scanout and VBLANK IRQ
    8, 0xFFFFFFFF, REGISTER_IDLE, CHECK_MODE, // MODE: documented bits, checked as a frame
    32, 0xFFFFFFFF, REGISTER_IDLE, CHECK_START, // START: checked as a frame
    40, 0x000000FF, REGISTER_IDLE, CHECK_NONE, // PALETTE_INDEX below 256
    44, 0x00FFFFFF, REGISTER_IDLE, CHECK_NONE, // PALETTE_DATA 24-bit RGB
    128, 0x00000000, REGISTER_IDLE, CHECK_NONE, // CURSOR_CONTROL: zero only
]

// Hardware classes. Pages with destructive reads, DMA authority or global
// effect are never user-mappable; only a page proven read-safe may be a
// DEVICE_KIND_MMIO row. The kernel keeps its own pages and IRQ lines.
let DEVICE_READ_SAFE_PAGES: UWord = 1 << ((VIDEO_BASE - IO_BASE) / PAGE_SIZE)
let DEVICE_KERNEL_PAGES: UWord = 1 | (1 << ((UART_BASE - IO_BASE) / PAGE_SIZE)) |
    (1 << ((TIMER_BASE - IO_BASE) / PAGE_SIZE)) | (1 << ((POWER_BASE - IO_BASE) / PAGE_SIZE)) |
    (1 << ((RNG_BASE - IO_BASE) / PAGE_SIZE)) // PIC is page 0
let DEVICE_IO_PAGES: UWord = 32

extern let __start_rodata: UByte
extern let __stop_rodata: UByte

let deviceField(row: UWord, column: UWord): UWord {
    return deviceRows[row * DEVICE_ROW_WORDS + column]
}

let deviceIoPage(physical: UWord): UWord {
    if physical < IO_BASE || physical & PAGE_MASK != 0 return DEVICE_IO_PAGES
    let page: UWord = (physical - IO_BASE) / PAGE_SIZE
    if page >= DEVICE_IO_PAGES return DEVICE_IO_PAGES
    return page
}

let deviceLineAllowed(line: UWord): Bool {
    return line < PIC_LINE_COUNT && line != TIMER_IRQ
}

// Embedded read-only blobs, by index. A blob must lie in kernel rodata.
// Both helpers return zero for an unknown or misplaced blob.
let deviceBlobStart(index: UWord): UWord {
    let first: UWord = &fontData as UWord
    let size: UWord = ((&fontDataEnd as UWord) - first + PAGE_MASK) & ~PAGE_MASK
    if index != 0 || first < (&__start_rodata as UWord) || first >= (&__stop_rodata as UWord) ||
        (&fontDataEnd as UWord) <= first || size > (&__stop_rodata as UWord) - first return 0
    return first
}
let deviceBlobBytes(index: UWord): UWord {
    if deviceBlobStart(index) == 0 return 0
    return ((&fontDataEnd as UWord) - (&fontData as UWord) + PAGE_MASK) & ~PAGE_MASK
}

let deviceRowValid(row: UWord): Bool {
    if row >= DEVICE_ROW_COUNT return false
    let kind: UWord = deviceField(row, 0)
    let virtual: UWord = deviceField(row, 2)
    let physical: UWord = deviceField(row, 3)
    let bytes: UWord = deviceField(row, 4)
    let line: UWord = deviceField(row, 5)
    if kind == DEVICE_KIND_VRAM {
        return physical >= VRAM_BASE && physical < IO_BASE && physical & PAGE_MASK == 0 &&
            bytes != 0 && bytes & PAGE_MASK == 0 && bytes <= IO_BASE - physical && virtual & PAGE_MASK == 0
    }
    if kind == DEVICE_KIND_MMIO {
        let page: UWord = deviceIoPage(physical)
        return page < DEVICE_IO_PAGES && bytes == PAGE_SIZE && virtual & PAGE_MASK == 0 &&
            DEVICE_READ_SAFE_PAGES & (1 << page) != 0 && DEVICE_KERNEL_PAGES & (1 << page) == 0
    }
    if kind == DEVICE_KIND_BLOB {
        return virtual & PAGE_MASK == 0 && bytes == 0 && deviceBlobStart(physical) != 0
    }
    if kind == DEVICE_KIND_IRQ return deviceLineAllowed(line)
    if kind == DEVICE_KIND_DISK {
        let page: UWord = deviceIoPage(physical)
        return page < DEVICE_IO_PAGES && DEVICE_KERNEL_PAGES & (1 << page) == 0 &&
            DEVICE_READ_SAFE_PAGES & (1 << page) == 0 && deviceLineAllowed(line)
    }
    return false
}

// Unrounded size of the role's blob, as announced in its start block. Zero if none.
let deviceRoleBlobBytes(role: UWord): UWord {
    for row: UWord in 0..DEVICE_ROW_COUNT {
        if deviceField(row, 0) == DEVICE_KIND_BLOB && deviceField(row, 1) == role && deviceRowValid(row) {
            return (&fontDataEnd as UWord) - (&fontData as UWord)
        }
    }
    return 0
}

// Virtual address of the role's blob mapping, or zero if the role has none.
let deviceRoleBlobVirtual(role: UWord): UWord {
    for row: UWord in 0..DEVICE_ROW_COUNT {
        if deviceField(row, 0) == DEVICE_KIND_BLOB && deviceField(row, 1) == role && deviceRowValid(row) {
            return deviceField(row, 2)
        }
    }
    return 0
}

let deviceRowMapping(row: UWord): Bool {
    let kind: UWord = deviceField(row, 0)
    let mapped: Bool = kind == DEVICE_KIND_VRAM || kind == DEVICE_KIND_MMIO || kind == DEVICE_KIND_BLOB
    return mapped && deviceRowValid(row)
}
// Concrete grant arguments of a mapping row; blobs resolve to their linked range.
let deviceRowVirtual(row: UWord): UWord { return deviceField(row, 2) }
let deviceRowPhysical(row: UWord): UWord {
    if deviceField(row, 0) == DEVICE_KIND_BLOB return deviceBlobStart(deviceField(row, 3))
    return deviceField(row, 3)
}
let deviceRowBytes(row: UWord): UWord {
    if deviceField(row, 0) == DEVICE_KIND_BLOB return deviceBlobBytes(deviceField(row, 3))
    return deviceField(row, 4)
}
let deviceRowPermissions(row: UWord): UWord {
    if deviceField(row, 0) == DEVICE_KIND_VRAM return PTE_RW | PTE_U
    return PTE_RO | PTE_U
}

// n-th mapping row of a role, as a row number, or DEVICE_ROW_COUNT.
let deviceRoleMapping(role: UWord, index: UWord): UWord {
    let mut seen: UWord = 0
    for row: UWord in 0..DEVICE_ROW_COUNT {
        if deviceField(row, 1) != role || !deviceRowMapping(row) continue
        if seen == index return row
        seen += 1
    }
    return DEVICE_ROW_COUNT
}
let deviceRoleMappingCount(role: UWord): UWord {
    let mut count: UWord = 0
    for row: UWord in 0..DEVICE_ROW_COUNT {
        if deviceField(row, 1) == role && deviceRowMapping(row) count += 1
    }
    return count
}

// A grant request must equal a valid row exactly: placement, size and permissions.
let deviceGrantAllowed(virtual: UWord, physical: UWord, bytes: UWord, permissions: UWord): Bool {
    for row: UWord in 0..DEVICE_ROW_COUNT {
        if deviceRowMapping(row) && deviceRowVirtual(row) == virtual && deviceRowPhysical(row) == physical &&
            deviceRowBytes(row) == bytes && deviceRowPermissions(row) == permissions return true
    }
    return false
}

// IRQ line the role's single interrupt row names, or DEVICE_NONE.
let deviceRoleIrq(role: UWord): UWord {
    for row: UWord in 0..DEVICE_ROW_COUNT {
        if deviceField(row, 0) == DEVICE_KIND_IRQ && deviceField(row, 1) == role && deviceRowValid(row) {
            return deviceField(row, 5)
        }
    }
    return DEVICE_NONE
}

let deviceDiskIrq(base: UWord): UWord {
    for row: UWord in 0..DEVICE_ROW_COUNT {
        if deviceField(row, 0) == DEVICE_KIND_DISK && deviceField(row, 3) == base && deviceRowValid(row) {
            return deviceField(row, 5)
        }
    }
    return DEVICE_NONE
}

// Boot may issue a line only if some valid row names it. The timer never is.
let deviceIrqAllowed(line: UWord): Bool {
    for row: UWord in 0..DEVICE_ROW_COUNT {
        let kind: UWord = deviceField(row, 0)
        let named: Bool = kind == DEVICE_KIND_IRQ || kind == DEVICE_KIND_DISK
        if named && deviceField(row, 5) == line && deviceRowValid(row) return true
    }
    return false
}

// Base of the role's register page and the size of its VRAM window. Zero when absent.
let deviceRoleRegisters(role: UWord): UWord {
    for row: UWord in 0..DEVICE_ROW_COUNT {
        if deviceField(row, 0) == DEVICE_KIND_MMIO && deviceField(row, 1) == role && deviceRowValid(row) {
            return deviceField(row, 3)
        }
    }
    return 0
}
let deviceRoleVramBytes(role: UWord): UWord {
    for row: UWord in 0..DEVICE_ROW_COUNT {
        if deviceField(row, 0) == DEVICE_KIND_VRAM && deviceField(row, 1) == role && deviceRowValid(row) {
            return deviceField(row, 4)
        }
    }
    return 0
}

// Returns the rule row for a register offset, or REGISTER_RULE_COUNT.
let deviceRegisterRule(offset: UWord): UWord {
    for rule: UWord in 0..REGISTER_RULE_COUNT {
        if registerRules[rule * REGISTER_RULE_WORDS] == offset return rule
    }
    return REGISTER_RULE_COUNT
}
let deviceRuleMask(rule: UWord): UWord { return registerRules[rule * REGISTER_RULE_WORDS + 1] }
let deviceRuleFlags(rule: UWord): UWord { return registerRules[rule * REGISTER_RULE_WORDS + 2] }
let deviceRuleCheck(rule: UWord): UWord { return registerRules[rule * REGISTER_RULE_WORDS + 3] }

export { DEVICE_ROLE_SCREEN, DEVICE_ROLE_INPUT, DEVICE_ROLE_STORAGE, DEVICE_ROLE_NET, DEVICE_NONE,
    DEVICE_ROW_COUNT, REGISTER_RULE_COUNT, REGISTER_IDLE, CHECK_NONE, CHECK_ENABLE,
    CHECK_MODE, CHECK_START, deviceRowValid, deviceRoleMapping, deviceRoleMappingCount,
    deviceRowVirtual, deviceRowPhysical, deviceRowBytes, deviceRowPermissions,
    deviceGrantAllowed, deviceRoleIrq, deviceDiskIrq,
    deviceIrqAllowed, deviceRoleRegisters, deviceRoleVramBytes, deviceRoleBlobBytes, deviceRoleBlobVirtual, deviceRegisterRule,
    deviceRuleMask, deviceRuleFlags, deviceRuleCheck, deviceBlobStart, deviceBlobBytes }

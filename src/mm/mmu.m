import { memoryGrantFrameOwner, memoryGrantMayProtect, memoryGrantUnmapped } from "sharing.m"
// Single-CPU MMU manager. All mutations hold memoryLock (save/restore IE).
// Shared supervisor physical window: user code frames become RO here before
// acquiring X anywhere. Task mappings occupy a separate virtual window.
import { kernelRamEnd, MAX_PAGES, PAGE_NONE, PAGE_DIRECTORY, PAGE_TABLE, PAGE_KERNEL, memorySlots,
    PAGE_USER, PAGE_USER_STACK, PAGE_KERNEL_STACK, allocPage, allocPageRun, freePage, physicalPageOwned,
    retainPage, releasePage, physicalPageReferences, pageAccessReferences,
    memoryLock, memoryUnlock } from "memory.m"
import { panic } from "../kernel/panic.m"
import { PAGE_SIZE, PAGE_MASK, PAGE_TABLE_ENTRIES, SUPERPAGE_SIZE, BOOT_INFO,
    BOOT_INFO_END, BOOT_LOAD, KERNEL_STACK_BYTES, PTE_V, PTE_R, PTE_W, PTE_X, PTE_U, PTE_RWX_BITS,
    PTE_RW, PTE_RX, PTE_RO, CR_PTBR, PTBR_ENABLE, TLBI_ALL, VRAM_BASE, IO_BASE, WORD_BITS,
    ERRNO_EFAULT, TASK_SLOT_MASK } from "../arch/wrm081632/defs.m"
import { deviceGrantAllowed, deviceRoleMappingCount } from "../drivers/device_table.m"

let USER_VA_START: UWord = 0x40000000
let USER_VA_END: UWord = 0xC0000000 // exclusive; all other VA belongs to the kernel
let MMU_KERNEL_OWNER: UWord = 0xFFFFFFFF // reserved; never assigned to a task
let PTE_AD: UWord = 0x60 // hardware-maintained A/D, never accepted from callers
let ACCESS_EXEC: UWord = 0x80000000
let ACCESS_COUNT: UWord = 0x7FFFFFFF
extern let kernelStackGuard: UByte
extern let kernelStackTop: UByte
extern let __start_text: UByte
extern let __stop_text: UByte
extern let __start_rodata: UByte
extern let __stop_rodata: UByte
extern let __start_data: UByte
extern let __bss_end: UByte
let mut kernelPageDirectory: *mut UWord
// Only initialized, pinned directories are accepted by the public API.
// The owner already lives in allocator records: only one initialization bit
// per physical frame is needed here, not another full-size owner array.
let mut spaceInitialized: UWord[MAX_PAGES / WORD_BITS]

// Frame quotas alone do not bound teardown: aliases need no new data frames.
// Charge ordinary leaves and private user tables independently. Bootstrap
// resources are descriptor rows; their tables still spend this quota.
let MMU_MAPPING_LIMIT: UWord = 128
let MMU_TABLE_LIMIT: UWord = 8
type SpaceBudget { directory: UWord, owner: UWord, mappings: UWord, tables: UWord }
let SPACE_BUDGET_BYTES: UWord = 16 // four words; test_task_tables checks it against the type
// One row per task slot, carved out of RAM by mmuInit (the count is memorySlots):
// an owner's row is the one its slot names whenever that is free, so a lookup is
// a single probe; the scan below it only serves owners that share a slot.
let mut spaceBudgets: *mut SpaceBudget
let mut spaceBudgetCount: UWord

let mmuBudget(directory: *mut UWord, owner: UWord): *mut SpaceBudget {
    let home: UWord = owner & TASK_SLOT_MASK
    if home != 0 && home <= spaceBudgetCount {
        let row: *mut SpaceBudget = &mut spaceBudgets[home - 1]
        if row.directory == (directory as UWord) && row.owner == owner return row
    }
    for i: UWord in 0..spaceBudgetCount {
        if spaceBudgets[i].directory == (directory as UWord) && spaceBudgets[i].owner == owner {
            return &mut spaceBudgets[i]
        }
    }
    return null
}

let mmuMappingAllows(directory: *mut UWord, owner: UWord, count: UWord): Bool {
    let budget: *SpaceBudget = mmuBudget(directory, owner)
    return budget != null && count <= MMU_MAPPING_LIMIT - budget.mappings
}

// Immutable bootstrap grants, separate from allocator-backed RAM aliases.
// No syscall exposes this constructor. Only exact device-table rows can be
// installed; a directory cannot impersonate a different owner.
type ResourceGrant {
    directory: UWord,
    owner: UWord,
    virtual: UWord,
    physical: UWord,
    bytes: UWord,
    permissions: UWord,
}
let MAX_RESOURCE_GRANTS: UWord = 8 // capacity; the device table decides how many are used
let mut resourceGrants: ResourceGrant[MAX_RESOURCE_GRANTS]
let mut resourcesSealed: Bool

let mmuResourceLeaf(directory: *mut UWord, owner: UWord, virtual: UWord, leaf: UWord): Bool {
    for i: UWord in 0..MAX_RESOURCE_GRANTS {
        let grant: *ResourceGrant = &resourceGrants[i]
        if grant.directory != (directory as UWord) || grant.owner != owner ||
            virtual < grant.virtual || virtual - grant.virtual >= grant.bytes continue
        return (leaf & ~PTE_AD) == (grant.physical + virtual - grant.virtual | grant.permissions)
    }
    return false
}

// True while no live directory holds this physical range. A dead owner keeps
// its grant until the task reaper destroys its directory, so this is also the
// observable "old owner fully reclaimed" check before a runtime regrant.
let mmuResourceFree(physical: UWord): Bool {
    for i: UWord in 0..MAX_RESOURCE_GRANTS {
        if resourceGrants[i].directory != 0 && resourceGrants[i].physical == physical return false
    }
    return true
}

// Boot-time constructor: closed by sealing. Runtime regrant of a role's rows
// goes through mmuInstallResource, which only the checked device factory calls.
let mmuGrantResource(directory: *mut UWord, owner: UWord, virtual: UWord,
    physical: UWord, bytes: UWord, permissions: UWord): Bool {
    if resourcesSealed return false
    return mmuInstallResource(directory, owner, virtual, physical, bytes, permissions)
}

let mmuInstallResource(directory: *mut UWord, owner: UWord, virtual: UWord,
    physical: UWord, bytes: UWord, permissions: UWord): Bool {
    let status: UWord = memoryLock()
    let valid: Bool = deviceGrantAllowed(virtual, physical, bytes, permissions)
    if !valid || !mmuSpaceOwned(directory, owner) || bytes == 0 ||
        virtual & PAGE_MASK != 0 || physical & PAGE_MASK != 0 || bytes & PAGE_MASK != 0 ||
        !mmuUserByteRangeValid(virtual, bytes) || bytes > SUPERPAGE_SIZE ||
        virtual / SUPERPAGE_SIZE != (virtual + bytes - 1) / SUPERPAGE_SIZE {
        memoryUnlock(status)
        return false
    }
    let mut slot: UWord = MAX_RESOURCE_GRANTS
    for i: UWord in 0..MAX_RESOURCE_GRANTS {
        // Resources are exclusive even under a different VA or directory.
        if resourceGrants[i].directory != 0 && resourceGrants[i].physical == physical {
            memoryUnlock(status)
            return false
        }
        if resourceGrants[i].directory == 0 slot = i
    }
    let budget: *mut SpaceBudget = mmuBudget(directory, owner)
    if slot == MAX_RESOURCE_GRANTS || directory[virtual / SUPERPAGE_SIZE] != 0 ||
        budget == null || budget.tables == MMU_TABLE_LIMIT {
        memoryUnlock(status)
        return false
    }
    let tableAddress: UWord = mmuAllocTable(owner)
    if tableAddress == PAGE_NONE {
        memoryUnlock(status)
        return false
    }
    let table: *mut UWord = tableAddress as *mut UWord
    for i: UWord in 0..(bytes / PAGE_SIZE) table[i] = physical + i * PAGE_SIZE | permissions
    resourceGrants[slot].directory = directory as UWord
    resourceGrants[slot].owner = owner
    resourceGrants[slot].virtual = virtual
    resourceGrants[slot].physical = physical
    resourceGrants[slot].bytes = bytes
    resourceGrants[slot].permissions = permissions
    fence()
    directory[virtual / SUPERPAGE_SIZE] = tableAddress | PTE_V
    budget.tables += 1
    mmuInvalidate()
    memoryUnlock(status)
    return true
}

let mmuSealResources(): Void { resourcesSealed = true }

// The owner must hold exactly the mapping rows of its role, each installed once.
let mmuResourcesValid(directory: *mut UWord, owner: UWord, role: UWord): Bool {
    let mut count: UWord = 0
    for i: UWord in 0..MAX_RESOURCE_GRANTS {
        let grant: *ResourceGrant = &resourceGrants[i]
        if grant.directory != (directory as UWord) || grant.owner != owner continue
        let entry: UWord = directory[grant.virtual / SUPERPAGE_SIZE]
        if !mmuPrivateTable(entry, owner) return false
        let table: *UWord = (entry & ~PAGE_MASK) as *UWord
        for page: UWord in 0..(grant.bytes / PAGE_SIZE) {
            if !mmuResourceLeaf(directory, owner, grant.virtual + page * PAGE_SIZE, table[page]) return false
        }
        count += 1
    }
    return count == deviceRoleMappingCount(role)
}

let kernelLayoutValid(): Bool {
    let textStart: UWord = &__start_text as UWord
    let rodataStart: UWord = &__start_rodata as UWord
    let dataStart: UWord = &__start_data as UWord
    let guard: UWord = &kernelStackGuard as UWord
    return textStart == BOOT_LOAD && rodataStart & PAGE_MASK == 0 &&
        dataStart & PAGE_MASK == 0 && (&__stop_text as UWord) <= rodataStart &&
        (&__stop_rodata as UWord) <= dataStart && rodataStart <= dataStart &&
        guard & PAGE_MASK == 0 && guard >= dataStart &&
        guard + PAGE_SIZE <= (&__bss_end as UWord) && (&__bss_end as UWord) <= SUPERPAGE_SIZE &&
        (&kernelStackTop as UWord) == guard + PAGE_SIZE + KERNEL_STACK_BYTES &&
        (&kernelStackTop as UWord) <= (&__bss_end as UWord)
}

// Kernel PTE permissions of a RAM page, 0 when it stays unmapped.
let kernelPagePermissions(address: UWord): UWord {
    // Page zero: a NULL load, store or call faults.
    if address < BOOT_INFO return 0
    // Boot info and the trap entry state.
    if address < BOOT_INFO_END return PTE_RW
    // The firmware's stack: unused once start.asm switches stacks.
    if address < BOOT_LOAD return 0
    if address < (&__start_rodata as UWord) return PTE_RX
    if address < (&__start_data as UWord) return PTE_RO
    if address == (&kernelStackGuard as UWord) return 0
    return PTE_RW
}

// Overflow-free validation of nonempty page ranges, including the last page.
let mmuUserRangeValid(address: UWord, size: UWord): Bool {
    return address & PAGE_MASK == 0 && size != 0 && size & PAGE_MASK == 0 &&
        address >= USER_VA_START && address < USER_VA_END && size <= USER_VA_END - address
}

let mmuInvalidate(): Void {
    // Also covers inactive ASIDs, permission upgrades and an entire superpage.
    fence()
    tlbi(0, TLBI_ALL)
}

// These operations cannot fail after validation under memoryLock. Treat a
// violated accounting invariant as fatal instead of continuing with stale PTEs.
let mmuRequire(ok: Bool): Void {
    if !ok panic("MMU reference accounting damaged", null)
}

let mmuAllocTable(owner: UWord): UWord {
    let address: UWord = allocPage(owner, PAGE_TABLE)
    if address == PAGE_NONE return PAGE_NONE
    let table: *mut UWord = address as *mut UWord
    for i: UWord in 0..PAGE_TABLE_ENTRIES table[i] = 0
    mmuRequire(retainPage(address, owner, PAGE_TABLE))
    return address
}

let mmuFreeTable(address: UWord, owner: UWord): Void {
    mmuRequire(releasePage(address, owner, PAGE_TABLE))
    mmuRequire(freePage(address, owner, PAGE_TABLE))
}

let mmuLedgerFree(ledger: UWord, pages: UWord): Void {
    for i: UWord in 0..pages mmuRequire(freePage(ledger + i * PAGE_SIZE, MMU_KERNEL_OWNER, PAGE_KERNEL))
}

let mmuInit(): Bool {
    let status: UWord = memoryLock()
    if kernelPageDirectory != null || mfcr(CR_PTBR) & PTBR_ENABLE != 0 ||
        kernelRamEnd == 0 || !kernelLayoutValid() || (&__bss_end as UWord) > kernelRamEnd {
        memoryUnlock(status)
        return false
    }
    let rows: UWord = memorySlots()
    let ledgerPages: UWord = (rows * SPACE_BUDGET_BYTES + PAGE_MASK) / PAGE_SIZE
    let ledger: UWord = allocPageRun(MMU_KERNEL_OWNER, PAGE_KERNEL, ledgerPages)
    if ledger == PAGE_NONE {
        memoryUnlock(status)
        return false
    }
    let root: UWord = allocPage(MMU_KERNEL_OWNER, PAGE_DIRECTORY)
    if root == PAGE_NONE {
        mmuLedgerFree(ledger, ledgerPages)
        memoryUnlock(status)
        return false
    }
    let directory: *mut UWord = root as *mut UWord
    for i: UWord in 0..PAGE_TABLE_ENTRIES directory[i] = 0
    let count: UWord = (kernelRamEnd + SUPERPAGE_SIZE - 1) / SUPERPAGE_SIZE
    for i: UWord in 0..count {
        let base: UWord = i * SUPERPAGE_SIZE
        // All RAM uses shared 4 KiB tables, so a physical-window permission
        // change reaches every existing and future space without RW copies.
        let tableAddress: UWord = mmuAllocTable(MMU_KERNEL_OWNER)
        if tableAddress == PAGE_NONE {
            for allocated: UWord in 0..i {
                mmuFreeTable(directory[allocated] & ~PAGE_MASK, MMU_KERNEL_OWNER)
            }
            mmuRequire(freePage(root, MMU_KERNEL_OWNER, PAGE_DIRECTORY))
            mmuLedgerFree(ledger, ledgerPages)
            memoryUnlock(status)
            return false
        }
        let table: *mut UWord = tableAddress as *mut UWord
        for page: UWord in 0..PAGE_TABLE_ENTRIES {
            let address: UWord = base + page * PAGE_SIZE
            let permissions: UWord = kernelPagePermissions(address)
            if address < kernelRamEnd && permissions != 0 table[page] = address | permissions
        }
        directory[i] = (table as UWord) | PTE_V
    }
    directory[VRAM_BASE / SUPERPAGE_SIZE] = VRAM_BASE | PTE_RW
    directory[IO_BASE / SUPERPAGE_SIZE] = IO_BASE | PTE_RW
    mmuRequire(retainPage(root, MMU_KERNEL_OWNER, PAGE_DIRECTORY))
    kernelPageDirectory = directory
    spaceBudgets = ledger as *mut SpaceBudget
    spaceBudgetCount = rows
    mmuInvalidate()
    mtcr(CR_PTBR, root | PTBR_ENABLE)
    memoryUnlock(status)
    return true
}

let mmuSpaceOwned(directory: *mut UWord, owner: UWord): Bool {
    let address: UWord = directory as UWord
    let page: UWord = address / PAGE_SIZE
    return kernelPageDirectory != null && owner != MMU_KERNEL_OWNER &&
        physicalPageOwned(address, owner, PAGE_DIRECTORY) &&
        spaceInitialized[page / WORD_BITS] & (1 as UWord << (page % WORD_BITS)) != 0 &&
        physicalPageReferences(address) == 1
}

// Compatibility for creation-time ledgers: a fresh allocated directory only.
let mmuInitAddressSpace(directory: *mut UWord, owner: UWord): Bool {
    let status: UWord = memoryLock()
    let address: UWord = directory as UWord
    let page: UWord = address / PAGE_SIZE
    if kernelPageDirectory == null || mfcr(CR_PTBR) & PTBR_ENABLE == 0 ||
        owner == MMU_KERNEL_OWNER || !physicalPageOwned(address, owner, PAGE_DIRECTORY) ||
        spaceInitialized[page / WORD_BITS] & (1 as UWord << (page % WORD_BITS)) != 0 ||
        physicalPageReferences(address) != 0 ||
        address == (mfcr(CR_PTBR) & ~PAGE_MASK) {
        memoryUnlock(status)
        return false
    }
    // The owner's own row when it is free, else the first free one.
    let mut budget: *mut SpaceBudget = null
    let home: UWord = owner & TASK_SLOT_MASK
    if home != 0 && home <= spaceBudgetCount && spaceBudgets[home - 1].directory == 0 budget = &mut spaceBudgets[home - 1]
    for i: UWord in 0..spaceBudgetCount {
        if budget == null && spaceBudgets[i].directory == 0 budget = &mut spaceBudgets[i]
    }
    if budget == null {
        memoryUnlock(status)
        return false
    }
    for i: UWord in 0..PAGE_TABLE_ENTRIES directory[i] = kernelPageDirectory[i]
    budget.directory = address
    budget.owner = owner
    budget.mappings = 0
    budget.tables = 0
    mmuRequire(retainPage(address, owner, PAGE_DIRECTORY))
    spaceInitialized[page / WORD_BITS] |= 1 as UWord << (page % WORD_BITS)
    memoryUnlock(status)
    return true
}

let mmuCreateAddressSpace(owner: UWord): UWord {
    let status: UWord = memoryLock()
    if owner == 0 || owner == MMU_KERNEL_OWNER || kernelPageDirectory == null {
        memoryUnlock(status)
        return PAGE_NONE
    }
    let address: UWord = allocPage(owner, PAGE_DIRECTORY)
    if address != PAGE_NONE && !mmuInitAddressSpace(address as *mut UWord, owner) {
        mmuRequire(freePage(address, owner, PAGE_DIRECTORY))
        memoryUnlock(status)
        return PAGE_NONE
    }
    memoryUnlock(status)
    return address
}

let mmuUserPurpose(address: UWord, owner: UWord): UWord {
    if physicalPageOwned(address, owner, PAGE_USER) return PAGE_USER
    if physicalPageOwned(address, owner, PAGE_USER_STACK) return PAGE_USER_STACK
    return PAGE_NONE
}

// An address-space owner is not necessarily the allocation owner. Foreign
// leaves need a matching explicit grant and exact borrower mapping ledger.
let mmuUserFrameOwner(owner: UWord, virtual: UWord, physical: UWord, permissions: UWord): UWord {
    if mmuUserPurpose(physical, owner) != PAGE_NONE return owner
    return memoryGrantFrameOwner(owner, virtual, physical, permissions)
}

let mmuPermissionsValid(permissions: UWord): Bool {
    // Require V/U/R, forbid caller A/D/G/reserved bits and writable code.
    return permissions & (PTE_V | PTE_U | PTE_R) == (PTE_V | PTE_U | PTE_R) &&
        permissions & ~(PTE_V | PTE_U | PTE_RWX_BITS) == 0 &&
        permissions & (PTE_W | PTE_X) != (PTE_W | PTE_X)
}

let mmuPrivateTable(entry: UWord, owner: UWord): Bool {
    return entry & PAGE_MASK == PTE_V && physicalPageOwned(entry & ~PAGE_MASK, owner, PAGE_TABLE) &&
        physicalPageReferences(entry & ~PAGE_MASK) == 1
}

// Remove the old leaf's access from a local count, without changing metadata.
// Only call for an owned frame, under memoryLock.
let mmuAccessRemaining(physical: UWord, previous: UWord): UWord {
    let mut access: UWord = pageAccessReferences[physical / PAGE_SIZE]
    if previous & (PTE_W | PTE_X) != 0 {
        mmuRequire(access & ACCESS_COUNT != 0 &&
            ((access & ACCESS_EXEC != 0) == (previous & PTE_X != 0)))
        access -= 1
        if access & ACCESS_COUNT == 0 access = 0
    }
    return access
}

let mmuAccessValid(physical: UWord, purpose: UWord, permissions: UWord,
    previous: UWord): Bool {
    // A stack is data even if a caller asks for RX instead of RWX.
    if purpose == PAGE_USER_STACK && permissions & PTE_X != 0 return false
    let access: UWord = mmuAccessRemaining(physical, previous)
    if permissions & (PTE_W | PTE_X) == 0 return true
    return access & ACCESS_COUNT != ACCESS_COUNT &&
        (access == 0 || ((access & ACCESS_EXEC != 0) == (permissions & PTE_X != 0)))
}

// Caller has validated conflicts and owns publication/invalidation ordering.
// Return whether the shared physical-window leaf changed. Never give this
// window U or X, and preserve the hardware A/D bits of the identity leaf.
let mmuChangeAccess(physical: UWord, permissions: UWord, previous: UWord): Bool {
    let mut access: UWord = mmuAccessRemaining(physical, previous)
    if permissions & (PTE_W | PTE_X) != 0 {
        access += 1
        if permissions & PTE_X != 0 access |= ACCESS_EXEC
    }
    pageAccessReferences[physical / PAGE_SIZE] = access
    let table: *mut UWord = (kernelPageDirectory[physical / SUPERPAGE_SIZE] & ~PAGE_MASK) as *mut UWord
    let index: UWord = physical / PAGE_SIZE % PAGE_TABLE_ENTRIES
    let mut flags: UWord = PTE_RW
    if access & ACCESS_EXEC != 0 flags = PTE_RO
    let leaf: UWord = physical | flags | (table[index] & PTE_AD)
    if table[index] == leaf return false
    table[index] = leaf
    return true
}

// Bounded all-or-nothing mapping. Stage missing tables without publishing
// parents; allocation failure leaves both PTEs and resource charges unchanged.
// The caller holds memoryLock and supplies a trusted unique frame ledger.
let mmuMapRegion(directory: *mut UWord, owner: UWord, virtual: UWord,
    pages: *UWord, count: UWord, permissions: UWord): Bool {
    if count == 0 || count > 16 || !mmuSpaceOwned(directory, owner) ||
        !mmuUserRangeValid(virtual, count * PAGE_SIZE) || !mmuPermissionsValid(permissions) ||
        !mmuMappingAllows(directory, owner, count) return false
    let budget: *mut SpaceBudget = mmuBudget(directory, owner)
    let first: UWord = virtual / SUPERPAGE_SIZE
    let last: UWord = (virtual + (count - 1) * PAGE_SIZE) / SUPERPAGE_SIZE
    // Sixteen pages can touch at most two directory slots.
    let mut staged: UWord[2]
    for i: UWord in 0..2 staged[i] = 0
    for i: UWord in 0..count {
        let physical: UWord = pages[i]
        let address: UWord = virtual + i * PAGE_SIZE
        let frameOwner: UWord = mmuUserFrameOwner(owner, address, physical, permissions)
        let purpose: UWord = mmuUserPurpose(physical, frameOwner)
        let entry: UWord = directory[address / SUPERPAGE_SIZE]
        if purpose == PAGE_NONE || !mmuAccessValid(physical, purpose, permissions, 0) ||
            physicalPageReferences(physical) == 0xFFFFFFFF return false
        for j: UWord in 0..i { if pages[j] == physical return false }
        if entry != 0 {
            if !mmuPrivateTable(entry, owner) return false
            let table: *UWord = (entry & ~PAGE_MASK) as *UWord
            if table[address / PAGE_SIZE % PAGE_TABLE_ENTRIES] != 0 return false
        }
    }
    let mut needed: UWord = 0
    for slot: UWord in first..(last + 1) { if directory[slot] == 0 needed += 1 }
    if needed > MMU_TABLE_LIMIT - budget.tables return false
    for slot: UWord in first..(last + 1) {
        if directory[slot] != 0 continue
        staged[slot - first] = mmuAllocTable(owner)
        if staged[slot - first] != PAGE_NONE continue
        for previous: UWord in first..slot {
            if staged[previous - first] != PAGE_NONE mmuFreeTable(staged[previous - first], owner)
        }
        return false
    }
    // No fallible operation remains. Revoke supervisor W before any X leaf.
    let mut changed: Bool = false
    for i: UWord in 0..count {
        let frameOwner: UWord = mmuUserFrameOwner(owner, virtual + i * PAGE_SIZE, pages[i], permissions)
        mmuRequire(retainPage(pages[i], frameOwner, mmuUserPurpose(pages[i], frameOwner)))
        if mmuChangeAccess(pages[i], permissions, 0) changed = true
    }
    if changed mmuInvalidate()
    for i: UWord in 0..count {
        let address: UWord = virtual + i * PAGE_SIZE
        let slot: UWord = address / SUPERPAGE_SIZE
        let mut tableAddress: UWord = directory[slot] & ~PAGE_MASK
        if tableAddress == 0 tableAddress = staged[slot - first]
        let table: *mut UWord = tableAddress as *mut UWord
        table[address / PAGE_SIZE % PAGE_TABLE_ENTRIES] = pages[i] | permissions
    }
    fence()
    for slot: UWord in first..(last + 1) {
        if staged[slot - first] != 0 directory[slot] = staged[slot - first] | PTE_V
    }
    budget.tables += needed
    budget.mappings += count
    mmuInvalidate()
    return true
}

let mapPage(directory: *mut UWord, owner: UWord, virtual: UWord,
    physical: UWord, permissions: UWord): Bool {
    let status: UWord = memoryLock()
    if !mmuSpaceOwned(directory, owner) || !mmuUserRangeValid(virtual, PAGE_SIZE) ||
        !mmuPermissionsValid(permissions) || mmuUserPurpose(physical, owner) == PAGE_NONE ||
        !mmuMappingAllows(directory, owner, 1) {
        memoryUnlock(status)
        return false
    }
    let slot: UWord = virtual / SUPERPAGE_SIZE
    let index: UWord = virtual / PAGE_SIZE % PAGE_TABLE_ENTRIES
    let entry: UWord = directory[slot]
    let budget: *mut SpaceBudget = mmuBudget(directory, owner)
    if entry == 0 && budget.tables == MMU_TABLE_LIMIT {
        memoryUnlock(status)
        return false
    }
    if entry != 0 && !mmuPrivateTable(entry, owner) {
        memoryUnlock(status)
        return false
    }
    let mut table: *mut UWord = (entry & ~PAGE_MASK) as *mut UWord
    if entry != 0 && table[index] != 0 {
        memoryUnlock(status)
        return false
    }
    let purpose: UWord = mmuUserPurpose(physical, owner)
    if !mmuAccessValid(physical, purpose, permissions, 0) {
        memoryUnlock(status)
        return false
    }
    if !retainPage(physical, owner, purpose) {
        memoryUnlock(status)
        return false
    }
    if entry == 0 {
        let address: UWord = mmuAllocTable(owner)
        if address == PAGE_NONE {
            mmuRequire(releasePage(physical, owner, purpose))
            memoryUnlock(status)
            return false
        }
        table = address as *mut UWord
        // Revoke cached supervisor W before publishing the first executable
        // alias. All fallible work has completed; no rollback can leave RO.
        if mmuChangeAccess(physical, permissions, 0) mmuInvalidate()
        table[index] = physical | permissions
        fence() // initialize the entire table before publishing the parent
        directory[slot] = address | PTE_V
        budget.tables += 1
    } else {
        if mmuChangeAccess(physical, permissions, 0) mmuInvalidate()
        table[index] = physical | permissions
    }
    budget.mappings += 1
    mmuInvalidate()
    memoryUnlock(status)
    return true
}

let mmuUserLeaf(directory: *mut UWord, owner: UWord, virtual: UWord): UWord {
    if !mmuSpaceOwned(directory, owner) || !mmuUserRangeValid(virtual, PAGE_SIZE) return 0
    let entry: UWord = directory[virtual / SUPERPAGE_SIZE]
    if !mmuPrivateTable(entry, owner) return 0
    let table: *UWord = (entry & ~PAGE_MASK) as *UWord
    let leaf: UWord = table[virtual / PAGE_SIZE % PAGE_TABLE_ENTRIES]
    if !mmuPermissionsValid((leaf & PAGE_MASK) & ~PTE_AD) ||
        mmuUserFrameOwner(owner, virtual, leaf & ~PAGE_MASK, leaf) == 0 ||
        physicalPageReferences(leaf & ~PAGE_MASK) == 0 return 0
    return leaf
}

// Byte ranges need neither page nor word alignment. Subtract before adding:
// no address+size wrap can disguise a range crossing the user window.
// Empty copies accept any address and never dereference either buffer.
let mmuUserByteRangeValid(address: UWord, size: UWord): Bool {
    return size == 0 || (address >= USER_VA_START && address < USER_VA_END &&
        size <= USER_VA_END - address)
}

// Under memoryLock: prove both user permissions and the supervisor alias
// before accessing data. mmuUserLeaf validates V/U/R, table/frame ownership
// and live references; inherited supervisor mappings and superpages fail.
let mmuUserBufferLeaf(directory: *mut UWord, owner: UWord, address: UWord,
    access: UWord): UWord {
    let leaf: UWord = mmuUserLeaf(directory, owner, address & ~PAGE_MASK)
    if leaf == 0 || leaf & access != access return 0
    let physical: UWord = leaf & ~PAGE_MASK
    let table: *UWord = (kernelPageDirectory[physical / SUPERPAGE_SIZE] & ~PAGE_MASK) as *UWord
    let alias: UWord = table[physical / PAGE_SIZE % PAGE_TABLE_ENTRIES]
    if alias & ~PAGE_MASK != physical || alias & (PTE_V | PTE_R | access) != (PTE_V | PTE_R | access) ||
        alias & (PTE_U | PTE_X) != 0 return 0
    return leaf
}

// Validate the ENTIRE range before any copy, and keep this same lock through
// the copy. A public validation result alone is not a pin for a later access.
let mmuUserBufferValidLocked(directory: *mut UWord, owner: UWord, address: UWord,
    size: UWord, access: UWord): Bool {
    if !mmuSpaceOwned(directory, owner) || !mmuUserByteRangeValid(address, size) ||
        access == 0 || access & ~(PTE_R | PTE_W) != 0 return false
    if size == 0 return true
    let last: UWord = (address + size - 1) & ~PAGE_MASK
    let mut page: UWord = address & ~PAGE_MASK
    while true {
        if mmuUserBufferLeaf(directory, owner, page, access) == 0 return false
        if page == last return true
        page += PAGE_SIZE
    }
}

let mmuUserBufferValid(directory: *mut UWord, owner: UWord, address: UWord,
    size: UWord, access: UWord): Bool {
    let status: UWord = memoryLock()
    let valid: Bool = mmuUserBufferValidLocked(directory, owner, address, size, access)
    memoryUnlock(status)
    return valid
}

// Kernel buffers are trusted, live, large enough, and disjoint from the user
// frames and MMU metadata. No allocation/scheduling inside the copy. On this
// single CPU memoryLock keeps mappings, rights and frame lifetime stable.
// Only proven physical aliases are accessed, including inside EXL=1; invalid
// user buffers return -EFAULT before changing the destination. No fault retry
// or nested trap protocol is involved. A bad kernel buffer remains a bug.
let copyFromUser(directory: *mut UWord, owner: UWord, destination: *mut UByte,
    source: UWord, size: UWord): Word {
    let status: UWord = memoryLock()
    if !mmuUserBufferValidLocked(directory, owner, source, size, PTE_R) {
        memoryUnlock(status)
        return -ERRNO_EFAULT
    }
    let mut copied: UWord = 0
    while copied < size {
        let address: UWord = source + copied
        let offset: UWord = address & PAGE_MASK
        let leaf: UWord = mmuUserBufferLeaf(directory, owner, address, PTE_R)
        let bytes: *UByte = ((leaf & ~PAGE_MASK) + offset) as *UByte
        let mut count: UWord = PAGE_SIZE - offset
        if count > size - copied count = size - copied
        for i: UWord in 0..count destination[copied + i] = bytes[i]
        copied += count
    }
    memoryUnlock(status)
    return 0
}

let copyToUser(directory: *mut UWord, owner: UWord, destination: UWord,
    source: *UByte, size: UWord): Word {
    let status: UWord = memoryLock()
    if !mmuUserBufferValidLocked(directory, owner, destination, size, PTE_W) {
        memoryUnlock(status)
        return -ERRNO_EFAULT
    }
    let mut copied: UWord = 0
    while copied < size {
        let address: UWord = destination + copied
        let offset: UWord = address & PAGE_MASK
        let leaf: UWord = mmuUserBufferLeaf(directory, owner, address, PTE_W)
        let bytes: *mut UByte = ((leaf & ~PAGE_MASK) + offset) as *mut UByte
        let mut count: UWord = PAGE_SIZE - offset
        if count > size - copied count = size - copied
        for i: UWord in 0..count bytes[i] = source[copied + i]
        copied += count
    }
    memoryUnlock(status)
    return 0
}

let unmapPage(directory: *mut UWord, owner: UWord, virtual: UWord): Bool {
    let status: UWord = memoryLock()
    let leaf: UWord = mmuUserLeaf(directory, owner, virtual)
    if leaf == 0 {
        memoryUnlock(status)
        return false
    }
    let frameOwner: UWord = mmuUserFrameOwner(owner, virtual, leaf & ~PAGE_MASK, leaf)
    let slot: UWord = virtual / SUPERPAGE_SIZE
    let address: UWord = directory[slot] & ~PAGE_MASK
    let table: *mut UWord = address as *mut UWord
    table[virtual / PAGE_SIZE % PAGE_TABLE_ENTRIES] = 0
    let mut empty: Bool = true
    for i: UWord in 0..PAGE_TABLE_ENTRIES {
        if table[i] != 0 empty = false
    }
    if empty directory[slot] = 0
    mmuInvalidate() // invalidate before returning any physical frame
    let physical: UWord = leaf & ~PAGE_MASK
    // The user translation is gone from all ASIDs before restoring kernel W.
    if mmuChangeAccess(physical, 0, leaf) mmuInvalidate()
    mmuRequire(releasePage(physical, frameOwner, mmuUserPurpose(physical, frameOwner)))
    memoryGrantUnmapped(owner, virtual, physical)
    let budget: *mut SpaceBudget = mmuBudget(directory, owner)
    mmuRequire(budget != null && budget.mappings != 0)
    budget.mappings -= 1
    if empty {
        mmuFreeTable(address, owner)
        mmuRequire(budget.tables != 0)
        budget.tables -= 1
    }
    // The leaf remains owned: the caller may remap it or freePage it now.
    memoryUnlock(status)
    return true
}

let setPagePermissions(directory: *mut UWord, owner: UWord, virtual: UWord,
    permissions: UWord): Bool {
    let status: UWord = memoryLock()
    let leaf: UWord = mmuUserLeaf(directory, owner, virtual)
    if leaf == 0 || !mmuPermissionsValid(permissions) {
        memoryUnlock(status)
        return false
    }
    let physical: UWord = leaf & ~PAGE_MASK
    let frameOwner: UWord = mmuUserFrameOwner(owner, virtual, physical, permissions)
    if frameOwner == 0 || (frameOwner != owner && !memoryGrantMayProtect(owner, virtual, physical, leaf, permissions)) ||
        !mmuAccessValid(physical, mmuUserPurpose(physical, frameOwner), permissions, leaf) {
        memoryUnlock(status)
        return false
    }
    let table: *mut UWord = (directory[virtual / SUPERPAGE_SIZE] & ~PAGE_MASK) as *mut UWord
    let index: UWord = virtual / PAGE_SIZE % PAGE_TABLE_ENTRIES
    if ((leaf & (PTE_W | PTE_X)) != (permissions & (PTE_W | PTE_X))) {
        // Break before make: no stale user X when kernel W is restored, and
        // no stale user/kernel W when the replacement becomes executable.
        table[index] = 0
        mmuInvalidate()
        if mmuChangeAccess(physical, permissions, leaf) mmuInvalidate()
    }
    table[index] = physical | permissions | (leaf & PTE_AD)
    mmuInvalidate()
    memoryUnlock(status)
    return true
}

// Split a task's inherited supervisor superpage into a private table, keeping
// every physical address and permission. Shared kernel tables are untouched.
// RAM already uses shared tables and cannot be split into stale RW copies.
// Kernel mappings cannot be edited through map/unmap/permission APIs.
let mmuSplitSuperpage(directory: *mut UWord, owner: UWord, virtual: UWord): Bool {
    let status: UWord = memoryLock()
    if !mmuSpaceOwned(directory, owner) || virtual & PAGE_MASK != 0 ||
        (virtual >= USER_VA_START && virtual < USER_VA_END) {
        memoryUnlock(status)
        return false
    }
    let slot: UWord = virtual / SUPERPAGE_SIZE
    let entry: UWord = directory[slot]
    // A/D may have been set in only one of the directories by the CPU.
    if entry & PTE_RWX_BITS == 0 || (entry & ~PTE_AD) != (kernelPageDirectory[slot] & ~PTE_AD) {
        memoryUnlock(status)
        return false
    }
    let address: UWord = mmuAllocTable(owner)
    if address == PAGE_NONE {
        memoryUnlock(status)
        return false
    }
    let table: *mut UWord = address as *mut UWord
    let base: UWord = entry & ~(SUPERPAGE_SIZE - 1)
    for i: UWord in 0..PAGE_TABLE_ENTRIES table[i] = (base + i * PAGE_SIZE) | (entry & PAGE_MASK)
    fence()
    directory[slot] = address | PTE_V
    mmuInvalidate()
    memoryUnlock(status)
    return true
}

// RAM tables are shared by EVERY root, including roots created earlier. Thus
// the guard and both old/new stacks keep identical supervisor mappings across
// PTBR switches. The guard's frame stays owned until the stack is released.
let mmuAllocKernelStack(owner: UWord): UWord {
    let status: UWord = memoryLock()
    if kernelPageDirectory == null || owner == 0 || owner == MMU_KERNEL_OWNER {
        memoryUnlock(status)
        return PAGE_NONE
    }
    let guard: UWord = allocPageRun(owner, PAGE_KERNEL_STACK, 1 + KERNEL_STACK_BYTES / PAGE_SIZE)
    if guard == PAGE_NONE {
        memoryUnlock(status)
        return PAGE_NONE
    }
    let table: *mut UWord = (kernelPageDirectory[guard / SUPERPAGE_SIZE] & ~PAGE_MASK) as *mut UWord
    table[guard / PAGE_SIZE % PAGE_TABLE_ENTRIES] = 0
    mmuInvalidate()
    memoryUnlock(status)
    return guard + PAGE_SIZE
}

// Only after CPU has left this stack AND its task directory. Restore the
// physical-window guard alias before returning its frame to the allocator.
let mmuFreeKernelStack(bottom: UWord, owner: UWord): Bool {
    let status: UWord = memoryLock()
    if kernelPageDirectory == null || bottom < PAGE_SIZE || bottom & PAGE_MASK != 0 {
        memoryUnlock(status)
        return false
    }
    let guard: UWord = bottom - PAGE_SIZE
    let count: UWord = 1 + KERNEL_STACK_BYTES / PAGE_SIZE
    for i: UWord in 0..count {
        if !physicalPageOwned(guard + i * PAGE_SIZE, owner, PAGE_KERNEL_STACK) ||
            physicalPageReferences(guard + i * PAGE_SIZE) != 0 {
            memoryUnlock(status)
            return false
        }
    }
    let table: *mut UWord = (kernelPageDirectory[guard / SUPERPAGE_SIZE] & ~PAGE_MASK) as *mut UWord
    if table[guard / PAGE_SIZE % PAGE_TABLE_ENTRIES] != 0 {
        memoryUnlock(status)
        return false
    }
    table[guard / PAGE_SIZE % PAGE_TABLE_ENTRIES] = guard | PTE_RW
    mmuInvalidate()
    for i: UWord in 0..count mmuRequire(freePage(guard + i * PAGE_SIZE, owner, PAGE_KERNEL_STACK))
    memoryUnlock(status)
    return true
}

let mmuSwitchAddressSpace(directory: *mut UWord, owner: UWord, asid: UWord): Bool {
    let status: UWord = memoryLock()
    if !mmuSpaceOwned(directory, owner) || asid > 255 {
        memoryUnlock(status)
        return false
    }
    fence()
    // Invalidate BEFORE a different-ASID PTBR can fetch a stale translation.
    // This deliberately forgoes ASID caching; no separate ASID lease registry.
    tlbi(0, TLBI_ALL)
    mtcr(CR_PTBR, (directory as UWord) | (asid << 4) | PTBR_ENABLE)
    memoryUnlock(status)
    return true
}

let mmuActivateKernel(): Bool {
    let status: UWord = memoryLock()
    if kernelPageDirectory == null {
        memoryUnlock(status)
        return false
    }
    fence()
    tlbi(0, TLBI_ALL)
    mtcr(CR_PTBR, (kernelPageDirectory as UWord) | PTBR_ENABLE)
    memoryUnlock(status)
    return true
}

// Trusted kernel owns table contents. Validate all private tables/leaves before
// teardown so wrong ownership cannot produce partial destruction.
let mmuDestroyAddressSpace(directory: *mut UWord, owner: UWord): Bool {
    let status: UWord = memoryLock()
    let root: UWord = directory as UWord
    if !mmuSpaceOwned(directory, owner) || root == (mfcr(CR_PTBR) & ~PAGE_MASK) {
        memoryUnlock(status)
        return false
    }
    for slot: UWord in 0..PAGE_TABLE_ENTRIES {
        let entry: UWord = directory[slot]
        if ((entry & ~PTE_AD) == (kernelPageDirectory[slot] & ~PTE_AD)) continue
        if !mmuPrivateTable(entry, owner) {
            memoryUnlock(status)
            return false
        }
        if slot >= USER_VA_START / SUPERPAGE_SIZE && slot < USER_VA_END / SUPERPAGE_SIZE {
            let table: *UWord = (entry & ~PAGE_MASK) as *UWord
            for i: UWord in 0..PAGE_TABLE_ENTRIES {
                if table[i] != 0 && mmuUserLeaf(directory, owner, slot * SUPERPAGE_SIZE + i * PAGE_SIZE) == 0 &&
                    !mmuResourceLeaf(directory, owner, slot * SUPERPAGE_SIZE + i * PAGE_SIZE, table[i]) {
                    memoryUnlock(status)
                    return false
                }
            }
        }
    }
    // Detach private parents, retaining their addresses in invalid entries for
    // the release pass. Flush AFTER this change and BEFORE freeing any frame.
    // The root is inactive and cannot be activated while memoryLock is held.
    for slot: UWord in 0..PAGE_TABLE_ENTRIES {
        let entry: UWord = directory[slot]
        if ((entry & ~PTE_AD) == (kernelPageDirectory[slot] & ~PTE_AD)) continue
        directory[slot] = entry & ~PTE_V
    }
    mmuInvalidate()
    let mut windowChanged: Bool = false
    for slot: UWord in 0..PAGE_TABLE_ENTRIES {
        let entry: UWord = directory[slot]
        if ((entry & ~PTE_AD) == (kernelPageDirectory[slot] & ~PTE_AD)) continue
        let address: UWord = entry & ~PAGE_MASK
        let table: *mut UWord = address as *mut UWord
        directory[slot] = 0
        if slot >= USER_VA_START / SUPERPAGE_SIZE && slot < USER_VA_END / SUPERPAGE_SIZE {
            for i: UWord in 0..PAGE_TABLE_ENTRIES {
                let physical: UWord = table[i] & ~PAGE_MASK
                if table[i] == 0 continue
                if mmuResourceLeaf(directory, owner, slot * SUPERPAGE_SIZE + i * PAGE_SIZE, table[i]) {
                    table[i] = 0
                    continue
                }
                let virtual: UWord = slot * SUPERPAGE_SIZE + i * PAGE_SIZE
                let frameOwner: UWord = mmuUserFrameOwner(owner, virtual, physical, table[i])
                let purpose: UWord = mmuUserPurpose(physical, frameOwner)
                if mmuChangeAccess(physical, 0, table[i]) windowChanged = true
                table[i] = 0
                mmuRequire(releasePage(physical, frameOwner, purpose))
                memoryGrantUnmapped(owner, virtual, physical)
                // Multiple aliases/spaces retain the frame until the last one.
                if frameOwner == owner && physicalPageReferences(physical) == 0 mmuRequire(freePage(physical, owner, purpose))
            }
        }
        mmuFreeTable(address, owner)
    }
    if windowChanged mmuInvalidate()
    for i: UWord in 0..MAX_RESOURCE_GRANTS {
        if resourceGrants[i].directory == root && resourceGrants[i].owner == owner resourceGrants[i].directory = 0
    }
    let page: UWord = root / PAGE_SIZE
    spaceInitialized[page / WORD_BITS] &= ~(1 as UWord << (page % WORD_BITS))
    let budget: *mut SpaceBudget = mmuBudget(directory, owner)
    mmuRequire(budget != null)
    budget.directory = 0
    budget.owner = 0
    budget.mappings = 0
    budget.tables = 0
    mmuRequire(releasePage(root, owner, PAGE_DIRECTORY))
    mmuRequire(freePage(root, owner, PAGE_DIRECTORY))
    memoryUnlock(status)
    return true
}

export { USER_VA_START, USER_VA_END, mmuUserRangeValid, mmuInit, mmuUserLeaf,
    mmuGrantResource, mmuInstallResource, mmuResourceFree, mmuSealResources, mmuResourcesValid,
    mmuUserByteRangeValid, mmuUserBufferValid, copyFromUser, copyToUser,
    mmuInitAddressSpace, mmuCreateAddressSpace, mapPage, unmapPage,
    setPagePermissions, mmuSplitSuperpage, mmuSwitchAddressSpace,
    mmuActivateKernel, mmuDestroyAddressSpace, mmuAllocKernelStack, mmuFreeKernelStack }

export { mmuMapRegion, mmuAccessValid, mmuPermissionsValid, mmuSpaceOwned }

export { mmuUserFrameOwner }

export { SpaceBudget, spaceBudgets, spaceBudgetCount, MMU_MAPPING_LIMIT, MMU_TABLE_LIMIT, mmuMappingAllows }

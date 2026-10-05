// Explicit whole-region grants. A lease pin is independent of each mapped
// leaf; exact task references and per-page bits survive either death order.
import { Task, currentTask, taskGet, TASK_DEAD } from "../task/task.m"
import { MemoryRegion, memoryRegionLookup, memorySpaceLookup, memoryRangeValid,
    memoryTryFreeOrphan } from "runtime.m"
import { PAGE_USER, retainPage, releasePage, physicalPageOwned,
    physicalPageReferences } from "memory.m"
import { mmuPermissionsValid, mmuAccessValid, mmuMapRegion, mmuUserLeaf } from "mmu.m"
import { objectAssertAtomic } from "../ipc/objects.m"
import { panic } from "../kernel/panic.m"
import { PAGE_SIZE, PTE_RWX_BITS, MEM_RIGHT_MAP, MEM_RIGHT_SHARE,
    ERRNO_EPERM, ERRNO_EINVAL, ERRNO_EBUSY, ERRNO_ENOMEM, ERRNO_ENFILE } from "../arch/wrm081632/defs.m"

let MAX_MEMORY_GRANTS: UWord = 64
let GRANT_GENERATION_MAX: UWord = 0x7FFFFF
type MemoryGrant {
    generation: UWord,
    lender: UWord,
    borrower: UWord,
    region: UWord,
    permissions: UWord,
    virtual: UWord,
    mapped: UWord, // one bit per whole-region frame, independent of lease pins
    open: Bool,
}
let mut memoryGrants: MemoryGrant[MAX_MEMORY_GRANTS]

let memoryGrantLookup(token: UWord): *mut MemoryGrant {
    let slot: UWord = token & 255
    if slot == 0 || slot > MAX_MEMORY_GRANTS return null
    let grant: *mut MemoryGrant = &mut memoryGrants[slot - 1]
    if grant.lender == 0 || grant.generation != token >> 8 return null
    return grant
}

// Called only after the last borrower leaf has been invalidated and unpinned.
let memoryGrantDrop(grant: *mut MemoryGrant): Void {
    if grant.mapped != 0 panic("dropping a mapped grant", null)
    let region: *mut MemoryRegion = memoryRegionLookup(grant.region, grant.lender)
    if region == null || region.grants == 0 panic("grant region ledger damaged", null)
    for i: UWord in 0..region.count {
        if !releasePage(region.pages[i], region.owner, PAGE_USER) panic("grant lease release failed", null)
    }
    region.grants -= 1
    grant.lender = 0
    grant.borrower = 0
    grant.region = 0
    grant.virtual = 0
    grant.open = false
    memoryTryFreeOrphan(region)
}

let memoryCreateGrant(space: UWord, regionToken: UWord, borrower: UWord, permissions: UWord): Word {
    let task: *mut Task = memorySpaceLookup(space, MEM_RIGHT_SHARE)
    if task == null || task != currentTask return -ERRNO_EPERM
    let region: *mut MemoryRegion = memoryRegionLookup(regionToken, task.id)
    let target: *mut Task = taskGet(borrower)
    if region == null || target == null || target.state == TASK_DEAD || target == task return -ERRNO_EPERM
    if !mmuPermissionsValid(permissions) return -ERRNO_EINVAL
    let mut lent: UWord = 0
    let mut slot: UWord = MAX_MEMORY_GRANTS
    for i: UWord in 0..MAX_MEMORY_GRANTS {
        let grant: *MemoryGrant = &memoryGrants[i]
        if grant.lender == task.id lent += 1
        if slot == MAX_MEMORY_GRANTS && grant.lender == 0 && grant.generation < GRANT_GENERATION_MAX slot = i
    }
    if lent >= 8 || slot == MAX_MEMORY_GRANTS return -ERRNO_ENFILE
    for i: UWord in 0..region.count {
        if !physicalPageOwned(region.pages[i], task.id, PAGE_USER) ||
            physicalPageReferences(region.pages[i]) == 0xFFFFFFFF return -ERRNO_EBUSY
    }
    let grant: *mut MemoryGrant = &mut memoryGrants[slot]
    grant.generation += 1
    grant.lender = task.id
    grant.borrower = borrower
    grant.region = regionToken
    grant.permissions = permissions
    grant.virtual = 0
    grant.mapped = 0
    grant.open = true
    for i: UWord in 0..region.count {
        if !retainPage(region.pages[i], task.id, PAGE_USER) panic("validated grant pin failed", null)
    }
    region.grants += 1
    return ((grant.generation << 8) | (slot + 1)) as Word
}

let memoryMapGrant(space: UWord, token: UWord, virtual: UWord, permissions: UWord): Word {
    let task: *mut Task = memorySpaceLookup(space, MEM_RIGHT_MAP)
    let grant: *mut MemoryGrant = memoryGrantLookup(token)
    if task == null || task != currentTask || grant == null || grant.borrower != task.id || !grant.open return -ERRNO_EPERM
    let region: *mut MemoryRegion = memoryRegionLookup(grant.region, grant.lender)
    if region == null return -ERRNO_EPERM
    if !memoryRangeValid(virtual, region.count) || !mmuPermissionsValid(permissions) return -ERRNO_EINVAL
    if permissions & PTE_RWX_BITS & ~(grant.permissions & PTE_RWX_BITS) != 0 return -ERRNO_EPERM
    if grant.mapped != 0 return -ERRNO_EBUSY
    // Offers and lease pins spend only the lender's quota. The borrower is
    // charged only after it explicitly accepts by mapping the grant.
    let mut borrowed: UWord = 0
    for i: UWord in 0..MAX_MEMORY_GRANTS {
        if memoryGrants[i].borrower == task.id && memoryGrants[i].mapped != 0 borrowed += 1
    }
    if borrowed >= 8 return -ERRNO_ENFILE
    for i: UWord in 0..region.count {
        if mmuUserLeaf(task.directory, task.id, virtual + i * PAGE_SIZE) != 0 ||
            !mmuAccessValid(region.pages[i], PAGE_USER, permissions, 0) return -ERRNO_EBUSY
    }
    // Stage authorization with the same IRQ exclusion as staged page tables.
    // Failed mapping clears this ledger; it never consumes the lease pins.
    grant.virtual = virtual
    grant.mapped = (1 as UWord << region.count) - 1
    if !mmuMapRegion(task.directory, task.id, virtual, &region.pages[0], region.count, permissions) {
        grant.virtual = 0
        grant.mapped = 0
        return -ERRNO_ENOMEM
    }
    return 0
}

let memoryCloseGrant(token: UWord): Word {
    objectAssertAtomic()
    let grant: *mut MemoryGrant = memoryGrantLookup(token)
    if grant == null || currentTask == null return -ERRNO_EPERM
    if currentTask.id == grant.borrower {
        if grant.mapped != 0 return -ERRNO_EBUSY
        memoryGrantDrop(grant)
        return 0
    }
    if currentTask.id != grant.lender return -ERRNO_EPERM
    // Revocation stops new mapping and permission upgrades. Existing leaves
    // remain valid until explicit unmap or borrower teardown (no remote edits).
    grant.open = false
    if grant.mapped == 0 memoryGrantDrop(grant)
    return 0
}

let memoryMappedGrant(borrower: UWord, virtual: UWord, physical: UWord): *mut MemoryGrant {
    for i: UWord in 0..MAX_MEMORY_GRANTS {
        let grant: *mut MemoryGrant = &mut memoryGrants[i]
        if grant.lender == 0 || grant.borrower != borrower || grant.virtual == 0 || virtual < grant.virtual continue
        let region: *mut MemoryRegion = memoryRegionLookup(grant.region, grant.lender)
        if region == null || virtual - grant.virtual >= region.count * PAGE_SIZE ||
            (virtual - grant.virtual) % PAGE_SIZE != 0 continue
        let page: UWord = (virtual - grant.virtual) / PAGE_SIZE
        if grant.mapped & (1 as UWord << page) != 0 && region.pages[page] == physical return grant
    }
    return null
}

let memoryGrantFrameOwner(borrower: UWord, virtual: UWord, physical: UWord, permissions: UWord): UWord {
    let grant: *mut MemoryGrant = memoryMappedGrant(borrower, virtual, physical)
    if grant == null || permissions & PTE_RWX_BITS & ~(grant.permissions & PTE_RWX_BITS) != 0 return 0
    if !physicalPageOwned(physical, grant.lender, PAGE_USER) return 0
    return grant.lender
}

let memoryGrantMayProtect(borrower: UWord, virtual: UWord, physical: UWord,
    previous: UWord, permissions: UWord): Bool {
    let grant: *mut MemoryGrant = memoryMappedGrant(borrower, virtual, physical)
    return grant != null && permissions & PTE_RWX_BITS & ~(grant.permissions & PTE_RWX_BITS) == 0 &&
        (grant.open || permissions & PTE_RWX_BITS & ~(previous & PTE_RWX_BITS) == 0)
}

// MMU callback occurs after TLBI and mapping-reference release, before reuse.
let memoryGrantUnmapped(borrower: UWord, virtual: UWord, physical: UWord): Void {
    let grant: *mut MemoryGrant = memoryMappedGrant(borrower, virtual, physical)
    if grant == null return
    grant.mapped &= ~(1 as UWord << ((virtual - grant.virtual) / PAGE_SIZE))
    if grant.mapped != 0 return
    grant.virtual = 0
    if !grant.open memoryGrantDrop(grant)
}

let memoryRevokeSharing(owner: UWord): Void {
    for i: UWord in 0..MAX_MEMORY_GRANTS {
        let grant: *mut MemoryGrant = &mut memoryGrants[i]
        if grant.lender == 0 || (grant.lender != owner && grant.borrower != owner) continue
        grant.open = false
        if grant.mapped == 0 memoryGrantDrop(grant)
    }
}

let memoryReapSharing(borrower: UWord): Void {
    for i: UWord in 0..MAX_MEMORY_GRANTS {
        let grant: *mut MemoryGrant = &mut memoryGrants[i]
        if grant.lender != 0 && grant.borrower == borrower memoryGrantDrop(grant)
    }
}

export { MemoryGrant, memoryGrants, memoryCreateGrant, memoryMapGrant, memoryCloseGrant,
    memoryGrantFrameOwner, memoryGrantMayProtect, memoryGrantUnmapped,
    memoryRevokeSharing, memoryReapSharing }

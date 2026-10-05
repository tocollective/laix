// Nontransferable task-local authority. Tokens select kernel rows, never frames
// or directories. Explicit grants independently pin shared allocations.
import { Task, currentTask, taskGet, TASK_CREATED, TASK_DEAD } from "../task/task.m"
import { taskControlLookup } from "../task/control.m"
import { objectAssertAtomic } from "../ipc/objects.m"
import { PAGE_NONE, PAGE_USER, allocPage, freePage, physicalPageOwned,
    physicalPageReferences, pageAccessReferences, memoryBudgetDetach, memoryBudgetFind } from "memory.m"
import { mmuMapRegion, mmuUserLeaf, mmuAccessValid, mmuPermissionsValid,
    mmuUserRangeValid, mmuUserBufferValid, copyFromUser, unmapPage,
    setPagePermissions, mmuSpaceOwned, mmuUserFrameOwner } from "mmu.m"
import { memoryRevokeSharing, memoryReapSharing, memoryGrantMayProtect } from "sharing.m"
import { panic } from "../kernel/panic.m"
import { PAGE_SIZE, PAGE_MASK, PTE_R, PTE_U, PTE_RW, ERRNO_EFAULT, TASK_RIGHT_CONFIGURE,
    MEM_RIGHT_ALLOC, MEM_RIGHT_MAP, MEM_RIGHT_UNMAP, MEM_RIGHT_PROTECT,
    MEM_RIGHT_POPULATE, MEM_RIGHT_ALL, MEM_REGION_PAGES, MEM_VA_START,
    MEM_VA_END, ERRNO_EPERM, ERRNO_EINVAL, ERRNO_ENOMEM, ERRNO_ENFILE,
    ERRNO_EBUSY } from "../arch/wrm081632/defs.m"

let MAX_MEMORY_SPACES: UWord = 32
let MAX_MEMORY_REGIONS: UWord = 64
let MEMORY_GENERATION_MAX: UWord = 0x7FFFFF

type MemorySpace {
    generation: UWord,
    caller: UWord,
    target: UWord,
    rights: UWord,
}
type MemoryRegion {
    generation: UWord,
    owner: UWord,
    count: UWord,
    grants: UWord,
    detached: Bool,
    pages: UWord[MEM_REGION_PAGES],
}
let mut memorySpaces: MemorySpace[MAX_MEMORY_SPACES]
let mut memoryRegions: MemoryRegion[MAX_MEMORY_REGIONS]
// At most 64 regions * 16 pages can outlive their original allocation owner.
let mut memoryOrphanPages: UWord

let memorySpaceLookup(token: UWord, rights: UWord): *mut Task {
    objectAssertAtomic()
    let slot: UWord = token & 255
    if currentTask == null || slot == 0 || slot > MAX_MEMORY_SPACES return null
    let cap: *MemorySpace = &memorySpaces[slot - 1]
    if cap.caller != currentTask.id || token >> 8 != cap.generation ||
        cap.rights & rights != rights return null
    let task: *mut Task = taskGet(cap.target)
    if task == null || task.state == TASK_DEAD || !mmuSpaceOwned(task.directory, task.id) return null
    // Foreign loader authority expires on publication, even before row cleanup.
    if task != currentTask && task.state != TASK_CREATED return null
    return task
}

let memoryRegionLookup(token: UWord, owner: UWord): *mut MemoryRegion {
    let slot: UWord = token & 255
    if slot == 0 || slot > MAX_MEMORY_REGIONS return null
    let region: *mut MemoryRegion = &mut memoryRegions[slot - 1]
    if region.owner != owner || owner == 0 || region.generation != token >> 8 return null
    return region
}

// Target 0 requests self authority. A foreign target needs CONFIGURE authority
// over an unpublished task, and never acquires authority over its supervisor.
let memoryOpenSpace(requested: UWord, rights: UWord): Word {
    objectAssertAtomic()
    if currentTask == null || rights == 0 || rights & ~MEM_RIGHT_ALL != 0 return -ERRNO_EINVAL
    let mut target: UWord = requested
    if target == 0 target = currentTask.id
    let task: *mut Task = taskGet(target)
    if task == null || task.state == TASK_DEAD return -ERRNO_EPERM
    if target != currentTask.id {
        if task.state != TASK_CREATED || taskControlLookup(target, TASK_RIGHT_CONFIGURE) == null return -ERRNO_EPERM
    } else if rights & MEM_RIGHT_POPULATE != 0 return -ERRNO_EPERM
    let mut held: UWord = 0
    for i: UWord in 0..MAX_MEMORY_SPACES {
        if memorySpaces[i].caller == currentTask.id held += 1
    }
    if held >= 4 return -ERRNO_ENFILE
    for i: UWord in 0..MAX_MEMORY_SPACES {
        let cap: *mut MemorySpace = &mut memorySpaces[i]
        if cap.caller != 0 || cap.generation == MEMORY_GENERATION_MAX continue
        cap.generation += 1
        cap.caller = currentTask.id
        cap.target = target
        cap.rights = rights
        return ((cap.generation << 8) | (i + 1)) as Word
    }
    return -ERRNO_ENFILE
}

let memoryCloseSpace(token: UWord): Word {
    if memorySpaceLookup(token, 0) == null return -ERRNO_EPERM
    memorySpaces[(token & 255) - 1].caller = 0
    return 0
}

// Publication drops all loader rows. Death drops caller authority immediately;
// frame reclamation waits for inactive PTBR, another stack and DMA quiescence.
let memoryRevokeLoader(target: UWord): Void {
    for i: UWord in 0..MAX_MEMORY_SPACES {
        if memorySpaces[i].target == target && memorySpaces[i].caller != target memorySpaces[i].caller = 0
    }
}
let memoryRevokeTask(owner: UWord): Void {
    memoryRevokeSharing(owner)
    for i: UWord in 0..MAX_MEMORY_SPACES {
        if memorySpaces[i].target == owner || memorySpaces[i].caller == owner memorySpaces[i].caller = 0
    }
}

let memoryAllocate(token: UWord, count: UWord): Word {
    let task: *mut Task = memorySpaceLookup(token, MEM_RIGHT_ALLOC)
    if task == null return -ERRNO_EPERM
    if count == 0 || count > MEM_REGION_PAGES return -ERRNO_EINVAL
    let mut owned: UWord = 0
    let mut slot: UWord = MAX_MEMORY_REGIONS
    for i: UWord in 0..MAX_MEMORY_REGIONS {
        if memoryRegions[i].owner == task.id owned += 1
        if slot == MAX_MEMORY_REGIONS && memoryRegions[i].owner == 0 &&
            memoryRegions[i].generation < MEMORY_GENERATION_MAX slot = i
    }
    // A task cannot monopolize the global region ledger.
    if owned >= 8 || slot == MAX_MEMORY_REGIONS return -ERRNO_ENFILE
    let region: *mut MemoryRegion = &mut memoryRegions[slot]
    region.generation += 1
    for i: UWord in 0..count {
        region.pages[i] = allocPage(task.id, PAGE_USER)
        if region.pages[i] != PAGE_NONE continue
        for j: UWord in 0..i {
            if !freePage(region.pages[j], task.id, PAGE_USER) panic("region rollback failed", null)
            region.pages[j] = PAGE_NONE
        }
        return -ERRNO_ENOMEM
    }
    region.owner = task.id
    region.count = count
    region.grants = 0
    region.detached = false
    return ((region.generation << 8) | (slot + 1)) as Word
}

let memoryRelease(token: UWord, regionToken: UWord): Word {
    let task: *mut Task = memorySpaceLookup(token, MEM_RIGHT_ALLOC)
    if task == null return -ERRNO_EPERM
    let region: *mut MemoryRegion = memoryRegionLookup(regionToken, task.id)
    if region == null return -ERRNO_EPERM
    if region.grants != 0 return -ERRNO_EBUSY
    // Allocation ownership is independent of every mapping and DMA pin.
    for i: UWord in 0..region.count {
        if !physicalPageOwned(region.pages[i], task.id, PAGE_USER) ||
            physicalPageReferences(region.pages[i]) != 0 ||
            pageAccessReferences[region.pages[i] / PAGE_SIZE] != 0 return -ERRNO_EBUSY
    }
    for i: UWord in 0..region.count {
        if !freePage(region.pages[i], task.id, PAGE_USER) panic("region release failed", null)
        region.pages[i] = PAGE_NONE
    }
    region.owner = 0
    region.count = 0
    return 0
}

let memoryRangeValid(virtual: UWord, count: UWord): Bool {
    return count != 0 && count <= MEM_REGION_PAGES &&
        mmuUserRangeValid(virtual, count * PAGE_SIZE) && virtual >= MEM_VA_START &&
        virtual < MEM_VA_END && count * PAGE_SIZE <= MEM_VA_END - virtual
}

let memoryMap(token: UWord, regionToken: UWord, virtual: UWord,
    offset: UWord, count: UWord, permissions: UWord): Word {
    let task: *mut Task = memorySpaceLookup(token, MEM_RIGHT_MAP)
    if task == null return -ERRNO_EPERM
    let region: *mut MemoryRegion = memoryRegionLookup(regionToken, task.id)
    if region == null return -ERRNO_EPERM
    if !memoryRangeValid(virtual, count) || offset > region.count ||
        count > region.count - offset || !mmuPermissionsValid(permissions) return -ERRNO_EINVAL
    // A conflict or occupied leaf is distinguishable from table exhaustion.
    for i: UWord in 0..count {
        if mmuUserLeaf(task.directory, task.id, virtual + i * PAGE_SIZE) != 0 ||
            !mmuAccessValid(region.pages[offset + i], PAGE_USER, permissions, 0) return -ERRNO_EBUSY
    }
    if !mmuMapRegion(task.directory, task.id, virtual, &region.pages[offset], count, permissions) return -ERRNO_ENOMEM
    return 0
}

let memoryRegionHasFrame(owner: UWord, physical: UWord): Bool {
    for i: UWord in 0..MAX_MEMORY_REGIONS {
        let region: *MemoryRegion = &memoryRegions[i]
        if region.owner != owner continue
        for j: UWord in 0..region.count { if region.pages[j] == physical return true }
    }
    return false
}

// Validate the complete span first. Fixed startup/code/stack and device grants
// are outside this authority even when the caller owns their address space.
let memoryEdit(token: UWord, virtual: UWord, count: UWord,
    permissions: UWord, protect: Bool): Word {
    let mut rights: UWord = MEM_RIGHT_UNMAP
    if protect rights = MEM_RIGHT_PROTECT
    let task: *mut Task = memorySpaceLookup(token, rights)
    if task == null return -ERRNO_EPERM
    if !memoryRangeValid(virtual, count) || (protect && !mmuPermissionsValid(permissions)) return -ERRNO_EINVAL
    for i: UWord in 0..count {
        let leaf: UWord = mmuUserLeaf(task.directory, task.id, virtual + i * PAGE_SIZE)
        if leaf == 0 return -ERRNO_EPERM
        let frameOwner: UWord = mmuUserFrameOwner(task.id, virtual + i * PAGE_SIZE, leaf & ~PAGE_MASK, leaf)
        if frameOwner == task.id && !memoryRegionHasFrame(task.id, leaf & ~PAGE_MASK) return -ERRNO_EPERM
        if protect && frameOwner != task.id && !memoryGrantMayProtect(task.id, virtual + i * PAGE_SIZE,
            leaf & ~PAGE_MASK, leaf, permissions) return -ERRNO_EPERM
        if protect && !mmuAccessValid(leaf & ~PAGE_MASK, PAGE_USER, permissions, leaf) return -ERRNO_EBUSY
    }
    for i: UWord in 0..count {
        let mut ok: Bool = false
        if protect ok = setPagePermissions(task.directory, task.id, virtual + i * PAGE_SIZE, permissions)
        else ok = unmapPage(task.directory, task.id, virtual + i * PAGE_SIZE)
        if !ok panic("validated memory edit failed", null)
    }
    return 0
}

// Bounded loader copying. The entire source and destination are checked before
// the first write. No writable supervisor access to executable frames exists.
let memoryPopulate(token: UWord, regionToken: UWord, offset: UWord,
    source: UWord, bytes: UWord): Word {
    let task: *mut Task = memorySpaceLookup(token, MEM_RIGHT_POPULATE)
    if task == null || task.state != TASK_CREATED return -ERRNO_EPERM
    let region: *mut MemoryRegion = memoryRegionLookup(regionToken, task.id)
    if region == null return -ERRNO_EPERM
    let length: UWord = region.count * PAGE_SIZE
    if bytes == 0 || offset >= length || bytes > length - offset return -ERRNO_EINVAL
    if !mmuUserBufferValid(currentTask.directory, currentTask.id, source, bytes, PTE_R) return -ERRNO_EFAULT
    let first: UWord = offset / PAGE_SIZE
    let last: UWord = (offset + bytes - 1) / PAGE_SIZE
    for i: UWord in first..(last + 1) {
        if !mmuAccessValid(region.pages[i], PAGE_USER, PTE_RW | PTE_U, 0) return -ERRNO_EBUSY
    }
    let mut done: UWord = 0
    while done < bytes {
        let position: UWord = offset + done
        let mut part: UWord = PAGE_SIZE - (position & PAGE_MASK)
        if part > bytes - done part = bytes - done
        let destination: *mut UByte = (region.pages[position / PAGE_SIZE] + (position & PAGE_MASK)) as *mut UByte
        if copyFromUser(currentTask.directory, currentTask.id, destination, source + done, part) != 0 {
            panic("validated loader copy failed", null)
        }
        done += part
    }
    return 0
}

// Called after inactive-space teardown. MMU has released mapped frames; the
// allocation ledger also owns never-mapped and explicitly unmapped frames.
let memoryReapRegions(owner: UWord): Void {
    memoryRevokeTask(owner)
    memoryReapSharing(owner)
    for i: UWord in 0..MAX_MEMORY_REGIONS {
        let region: *mut MemoryRegion = &mut memoryRegions[i]
        if region.owner != owner continue
        if region.grants != 0 {
            if !memoryBudgetDetach(owner, region.count) panic("orphan budget detach failed", null)
            region.detached = true
            memoryOrphanPages += region.count
            continue
        }
        for j: UWord in 0..region.count {
            if physicalPageOwned(region.pages[j], owner, PAGE_USER) &&
                !freePage(region.pages[j], owner, PAGE_USER) panic("region remains pinned at reap", null)
            region.pages[j] = PAGE_NONE
        }
        region.owner = 0
        region.count = 0
    }
}

export { MemorySpace, MemoryRegion, memorySpaces, memoryRegions, memoryOpenSpace,
    memoryCloseSpace, memoryAllocate, memoryRelease, memoryMap, memoryEdit,
    memoryPopulate, memoryRevokeLoader, memoryRevokeTask, memoryReapRegions }

// Only orphaned allocations are reclaimed here; live owners retain their
// unmapped allocations. A DMA pin also delays orphan release after grant drop.
let memoryTryFreeOrphan(region: *mut MemoryRegion): Void {
    if !region.detached || region.grants != 0 || memoryBudgetFind(region.owner) != null return
    for i: UWord in 0..region.count {
        if physicalPageReferences(region.pages[i]) != 0 ||
            pageAccessReferences[region.pages[i] / PAGE_SIZE] != 0 return
    }
    for i: UWord in 0..region.count {
        if !freePage(region.pages[i], region.owner, PAGE_USER) panic("orphan release failed", null)
        region.pages[i] = PAGE_NONE
    }
    memoryOrphanPages -= region.count
    region.owner = 0
    region.count = 0
    region.detached = false
}

let memoryReapOrphans(): Void {
    for i: UWord in 0..MAX_MEMORY_REGIONS memoryTryFreeOrphan(&mut memoryRegions[i])
}

export { memorySpaceLookup, memoryRegionLookup, memoryRangeValid, memoryRegionHasFrame,
    memoryOrphanPages, memoryTryFreeOrphan, memoryReapOrphans }

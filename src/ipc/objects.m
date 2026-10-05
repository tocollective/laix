import { Task, taskGet } from "../task/task.m"
// Kernel-owned objects and capability tables. No user pointer enters this API.
import { CR_STATUS, STATUS_IE, STATUS_EXL, WORD_MASK, ERRNO_EINVAL,
    ERRNO_EBADF, ERRNO_EPERM, ERRNO_EMFILE, ERRNO_ENFILE, ERRNO_EPIPE,
    RIGHT_RECEIVE, RIGHT_MANAGE, RIGHT_ALL } from "../arch/wrm081632/defs.m"
import { ipcCancelEndpoint, ipcCancelTask } from "ipc.m"
import { transferReleaseTask } from "transfer.m"
import { panic } from "../kernel/panic.m"

let MAX_ENDPOINTS: UWord = 16
let MAX_HANDLES: UWord = 16
let ENDPOINT_WAIT_CAPACITY: UWord = 8 // one wait per task
let ENDPOINT_EMPTY: UWord = 0
let ENDPOINT_LIVE: UWord = 1
let ENDPOINT_DESTROYED: UWord = 2
let ENDPOINT_RETIRED: UWord = 3
let HANDLE_SLOT_BITS: UWord = 8
let HANDLE_SLOT_MASK: UWord = 255
let HANDLE_GENERATION_MAX: UWord = 0x7FFFFF // tokens fit a positive Word result
let ENDPOINT_RAW: UWord = 0
let ENDPOINT_SERVICE: UWord = 1

type Endpoint {
    id: UWord,
    generation: UWord,
    state: UWord,
    references: UWord,
    manager: UWord, // immutable Service receiver (Raw: creator)
    creator: UWord, // destruction authority and lifetime budget owner
    mode: UWord, // immutable until this object generation is released
    receiveReferences: UWord,
    senders: UWord[ENDPOINT_WAIT_CAPACITY],
    senderHead: UWord,
    senderCount: UWord,
    receivers: UWord[ENDPOINT_WAIT_CAPACITY],
    receiverHead: UWord,
    receiverCount: UWord,
}

type Handle {
    object: *mut Endpoint,
    objectGeneration: UWord,
    generation: UWord,
    rights: UWord,
    reserved: Bool, // receiver-owned transfer reservation; no endpoint reference
    receiveReference: Bool, // counts usable receive rights, not factory staging
}

type HandleTable {
    entries: Handle[MAX_HANDLES],
    factoryModes: UWord, // mode bits; bootstrap-only, nontransferable
    factoryQuota: UWord,
    factoryRecovery: Bool,
}

let mut endpoints: Endpoint[MAX_ENDPOINTS]
let mut bootstrapSealed: Bool
let ENDPOINT_RECOVERY_RESERVE: UWord = 2
let ENDPOINT_FACTORY_QUOTA: UWord = 12

// Single CPU: all table, reference and lifecycle mutations are indivisible
// with respect to timer IRQs. Checked pointers must stay inside this region.
let objectAssertAtomic(): Void {
    let status: UWord = mfcr(CR_STATUS)
    if status & STATUS_IE != 0 && status & STATUS_EXL == 0 {
        panic("endpoint operation with IRQs enabled", null)
    }
}

let handleEntry(table: *mut HandleTable, token: UWord): *mut Handle {
    objectAssertAtomic()
    let slot: UWord = token & HANDLE_SLOT_MASK
    let generation: UWord = token >> HANDLE_SLOT_BITS
    if table == null || slot == 0 || slot > MAX_HANDLES || generation == 0 ||
        generation > HANDLE_GENERATION_MAX return null
    let entry: *mut Handle = &mut table.entries[slot - 1]
    if entry.object == null || entry.generation != generation return null
    return entry
}

// Look up ONLY in the supplied task's table; endpoint IDs are never handles.
// Rights=0 is a lifecycle lookup, not an authorization for send/receive/manage.
let handleLookup(table: *mut HandleTable, token: UWord, rights: UWord): *mut Endpoint {
    let entry: *mut Handle = handleEntry(table, token)
    if entry == null || rights & ~RIGHT_ALL != 0 || entry.rights & rights != rights return null
    let object: *mut Endpoint = entry.object
    if object.generation != entry.objectGeneration || object.state != ENDPOINT_LIVE return null
    return object
}

let handleInstallScoped(table: *mut HandleTable, object: *mut Endpoint, rights: UWord, recovery: Bool): Word {
    objectAssertAtomic()
    if table == null || object == null || object.state != ENDPOINT_LIVE ||
        rights == 0 || rights & ~RIGHT_ALL != 0 return -ERRNO_EINVAL
    for i: UWord in 0..MAX_HANDLES {
        if table.factoryRecovery && !recovery && i >= MAX_HANDLES - ENDPOINT_RECOVERY_RESERVE continue
        let entry: *mut Handle = &mut table.entries[i]
        if entry.object != null || entry.reserved || entry.generation == HANDLE_GENERATION_MAX continue
        return handleInstallAt(table, object, rights, i + 1)
    }
    return -ERRNO_EMFILE
}

// Caller validates policy and owns this free slot, including its reservation.
let handleInstallAt(table: *mut HandleTable, object: *mut Endpoint, rights: UWord, slot: UWord): Word {
    objectAssertAtomic()
    if table == null || object == null || object.state != ENDPOINT_LIVE || slot == 0 ||
        slot > MAX_HANDLES || rights == 0 || rights & ~RIGHT_ALL != 0 return -ERRNO_EINVAL
    let entry: *mut Handle = &mut table.entries[slot - 1]
    if entry.object != null || entry.generation == HANDLE_GENERATION_MAX return -ERRNO_EMFILE
    // Advance on allocation, never reset on close or task teardown.
    entry.generation += 1
    entry.object = object
    entry.objectGeneration = object.generation
    entry.rights = rights
    entry.receiveReference = false
    if rights & RIGHT_RECEIVE != 0 {
        let receiver: *mut Task = taskGet(object.manager)
        if object.mode == ENDPOINT_RAW ||
            (receiver != null && table == &mut receiver.handles) {
            entry.receiveReference = true
            object.receiveReferences += 1
        }
    }
    object.references += 1 // bounded by handles plus one wait per task
    return ((entry.generation << HANDLE_SLOT_BITS) | slot) as Word
}

let handleInstall(table: *mut HandleTable, object: *mut Endpoint, rights: UWord): Word {
    return handleInstallScoped(table, object, rights, false)
}

let endpointRelease(object: *mut Endpoint): Void {
    if object.references != 0 return
    // Waits own references too; zero references must imply empty queues.
    if object.senderCount != 0 || object.receiverCount != 0 {
        panic("unreferenced endpoint with waiters", null)
        return
    }
    // The last reference closes the object even if no manager handle remains.
    object.manager = 0
    object.creator = 0
    if object.generation == WORD_MASK object.state = ENDPOINT_RETIRED
    else object.state = ENDPOINT_EMPTY
}

let handleDrop(entry: *mut Handle): Void {
    let object: *mut Endpoint = entry.object
    if object.generation != entry.objectGeneration || object.references == 0 {
        panic("invalid endpoint reference", null)
        return
    }
    if entry.receiveReference {
        if object.receiveReferences == 0 {
            panic("invalid receive reference", null)
            return
        }
        object.receiveReferences -= 1
        if object.mode == ENDPOINT_SERVICE && object.receiveReferences == 0 && object.state == ENDPOINT_LIVE {
            object.state = ENDPOINT_DESTROYED
            ipcCancelEndpoint(object)
        }
    }
    entry.object = null
    entry.objectGeneration = 0
    entry.rights = 0
    entry.receiveReference = false
    object.references -= 1
    endpointRelease(object)
}

let handleClose(table: *mut HandleTable, token: UWord): Word {
    let entry: *mut Handle = handleEntry(table, token)
    if entry == null return -ERRNO_EBADF
    // Destroyed objects remain pinned until every stale copy is closed.
    handleDrop(entry)
    return 0
}

// Common attenuation/liveness policy for trusted setup and receiver consent.
let handleCopyCheck(source: *mut HandleTable, token: UWord,
    sourceOwner: UWord, targetOwner: UWord, rights: UWord): Word {
    let entry: *mut Handle = handleEntry(source, token)
    if entry == null return -ERRNO_EBADF
    if rights == 0 || rights & ~RIGHT_ALL != 0 return -ERRNO_EINVAL
    if entry.rights & rights != rights return -ERRNO_EPERM
    let object: *mut Endpoint = handleLookup(source, token, rights)
    if object == null return -ERRNO_EPIPE
    // Creation authority is never copied. Management stays with the creator.
    if rights & RIGHT_MANAGE != 0 &&
        (sourceOwner != object.creator || targetOwner != object.creator) return -ERRNO_EPERM
    if object.mode == ENDPOINT_SERVICE && rights & RIGHT_RECEIVE != 0 &&
        (targetOwner != object.manager ||
         (sourceOwner != object.creator && sourceOwner != object.manager)) return -ERRNO_EPERM
    return 0
}

// Kernel-only setup/resolution API. Public IPC must establish consent first.
let handleCopy(source: *mut HandleTable, token: UWord, target: *mut HandleTable,
    sourceOwner: UWord, targetOwner: UWord, rights: UWord): Word {
    let checked: Word = handleCopyCheck(source, token, sourceOwner, targetOwner, rights)
    if checked != 0 return checked
    return handleInstall(target, handleLookup(source, token, rights), rights)
}

let endpointDestroy(table: *mut HandleTable, token: UWord, owner: UWord): Word {
    let entry: *mut Handle = handleEntry(table, token)
    if entry == null return -ERRNO_EBADF
    if entry.rights & RIGHT_MANAGE == 0 return -ERRNO_EPERM
    let object: *mut Endpoint = handleLookup(table, token, RIGHT_MANAGE)
    if object == null return -ERRNO_EPIPE
    if object.creator != owner return -ERRNO_EPERM
    object.state = ENDPOINT_DESTROYED
    ipcCancelEndpoint(object)
    return 0
}

// Internal construction: publish only after root installation succeeds.
let endpointAllocate(table: *mut HandleTable, owner: UWord, receiver: UWord,
    mode: UWord, recovery: Bool): Word {
    objectAssertAtomic()
    if table == null || owner == 0 || receiver == 0 || mode > ENDPOINT_SERVICE return -ERRNO_EINVAL
    for i: UWord in 0..MAX_ENDPOINTS {
        let object: *mut Endpoint = &mut endpoints[i]
        if !recovery && i >= MAX_ENDPOINTS - ENDPOINT_RECOVERY_RESERVE continue
        if object.state != ENDPOINT_EMPTY continue
        if object.generation == WORD_MASK {
            object.state = ENDPOINT_RETIRED
            continue
        }
        object.id = i + 1
        object.generation += 1
        object.state = ENDPOINT_LIVE
        object.references = 0
        object.manager = receiver
        object.creator = owner
        object.mode = mode
        object.receiveReferences = 0
        object.senderHead = 0
        object.senderCount = 0
        object.receiverHead = 0
        object.receiverCount = 0
        for j: UWord in 0..ENDPOINT_WAIT_CAPACITY {
            object.senders[j] = 0
            object.receivers[j] = 0
        }
        let token: Word = handleInstallScoped(table, object, RIGHT_ALL, recovery)
        if token < 0 endpointRelease(object)
        return token
    }
    return -ERRNO_ENFILE
}

// Bootstrap remains the sole root issuer, including factory policy.
let endpointBootstrapMode(table: *mut HandleTable, owner: UWord, mode: UWord): Word {
    objectAssertAtomic()
    if bootstrapSealed || owner == 0 return -ERRNO_EPERM
    return endpointAllocate(table, owner, owner, mode, true)
}

let endpointFactoryBootstrap(table: *mut HandleTable, modes: UWord, quota: UWord,
    recovery: Bool): Bool {
    objectAssertAtomic()
    if bootstrapSealed || table == null || table.factoryModes != 0 || modes == 0 ||
        modes > 3 || quota == 0 || quota > ENDPOINT_FACTORY_QUOTA return false
    table.factoryModes = modes
    table.factoryQuota = quota
    table.factoryRecovery = recovery
    return true
}

// Count destroyed-but-pinned objects too: stale copies cannot evade quotas.
let endpointFactoryCreate(table: *mut HandleTable, owner: UWord, receiver: UWord,
    mode: UWord): Word {
    objectAssertAtomic()
    if table == null || table.factoryModes == 0 return -ERRNO_EPERM
    if mode > ENDPOINT_SERVICE return -ERRNO_EINVAL
    if table.factoryModes & (1 as UWord << mode) == 0 return -ERRNO_EPERM
    let mut charged: UWord = 0
    for i: UWord in 0..MAX_ENDPOINTS {
        if endpoints[i].creator == owner && endpoints[i].references != 0 charged += 1
    }
    if charged >= table.factoryQuota return -ERRNO_ENFILE
    return endpointAllocate(table, owner, receiver, mode, table.factoryRecovery)
}

let endpointBootstrap(table: *mut HandleTable, owner: UWord): Word {
    return endpointBootstrapMode(table, owner, ENDPOINT_RAW)
}

// Trusted bootstrap only; receive authority stays with this management owner.
let endpointBootstrapService(table: *mut HandleTable, owner: UWord): Word {
    return endpointBootstrapMode(table, owner, ENDPOINT_SERVICE)
}

let endpointSealBootstrap(): Void {
    objectAssertAtomic()
    bootstrapSealed = true
}

let handlesReleaseTask(table: *mut HandleTable, owner: UWord): Void {
    objectAssertAtomic()
    ipcCancelTask(owner)
    table.factoryModes = 0
    table.factoryQuota = 0
    table.factoryRecovery = false
    transferReleaseTask(owner)
    // Creator or receiver death revokes peer handles even if its manage handle was closed.
    for i: UWord in 0..MAX_ENDPOINTS {
        if endpoints[i].state == ENDPOINT_LIVE && (endpoints[i].creator == owner || endpoints[i].manager == owner) {
            endpoints[i].state = ENDPOINT_DESTROYED
            ipcCancelEndpoint(&mut endpoints[i])
        }
    }
    for i: UWord in 0..MAX_HANDLES {
        if table.entries[i].object != null handleDrop(&mut table.entries[i])
    }
}

export { Endpoint, Handle, HandleTable, MAX_ENDPOINTS, MAX_HANDLES,
    ENDPOINT_EMPTY, ENDPOINT_LIVE, ENDPOINT_DESTROYED, ENDPOINT_RETIRED,
    ENDPOINT_RAW, ENDPOINT_SERVICE, ENDPOINT_FACTORY_QUOTA, ENDPOINT_RECOVERY_RESERVE,
    endpointFactoryBootstrap, endpointFactoryCreate,
    HANDLE_GENERATION_MAX, ENDPOINT_WAIT_CAPACITY, handleInstallAt, handleEntry, endpointRelease, handleLookup, handleClose, handleCopyCheck, handleCopy, endpointDestroy,
    endpointBootstrap, endpointBootstrapService, endpointSealBootstrap, handlesReleaseTask, objectAssertAtomic }

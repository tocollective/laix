// Kernel-owned objects and capability tables. No user pointer enters this API.
import { CR_STATUS, STATUS_IE, STATUS_EXL, WORD_MASK, ERRNO_EINVAL,
    ERRNO_EBADF, ERRNO_EPERM, ERRNO_EMFILE, ERRNO_ENFILE, ERRNO_EPIPE,
    RIGHT_RECEIVE, RIGHT_MANAGE, RIGHT_ALL } from "../arch/wrm081632/defs.m"
import { ipcCancelEndpoint, ipcCancelTask } from "ipc.m"
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
    manager: UWord,
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
}

type HandleTable {
    entries: Handle[MAX_HANDLES],
}

let mut endpoints: Endpoint[MAX_ENDPOINTS]
let mut bootstrapSealed: Bool

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

let handleInstall(table: *mut HandleTable, object: *mut Endpoint, rights: UWord): Word {
    objectAssertAtomic()
    if table == null || object == null || object.state != ENDPOINT_LIVE ||
        rights == 0 || rights & ~RIGHT_ALL != 0 return -ERRNO_EINVAL
    for i: UWord in 0..MAX_HANDLES {
        let entry: *mut Handle = &mut table.entries[i]
        if entry.object != null || entry.generation == HANDLE_GENERATION_MAX continue
        // Advance on allocation, never reset on close or task teardown.
        entry.generation += 1
        entry.object = object
        entry.objectGeneration = object.generation
        entry.rights = rights
        if rights & RIGHT_RECEIVE != 0 object.receiveReferences += 1
        object.references += 1 // bounded by handles plus one wait per task
        return ((entry.generation << HANDLE_SLOT_BITS) | (i + 1)) as Word
    }
    return -ERRNO_EMFILE
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
    if object.generation == WORD_MASK object.state = ENDPOINT_RETIRED
    else object.state = ENDPOINT_EMPTY
}

let handleDrop(entry: *mut Handle): Void {
    let object: *mut Endpoint = entry.object
    if object.generation != entry.objectGeneration || object.references == 0 {
        panic("invalid endpoint reference", null)
        return
    }
    if entry.rights & RIGHT_RECEIVE != 0 {
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

let handleCopy(source: *mut HandleTable, token: UWord, target: *mut HandleTable,
    sourceOwner: UWord, targetOwner: UWord, rights: UWord): Word {
    let entry: *mut Handle = handleEntry(source, token)
    if entry == null return -ERRNO_EBADF
    if rights == 0 || rights & ~RIGHT_ALL != 0 return -ERRNO_EINVAL
    if entry.rights & rights != rights return -ERRNO_EPERM
    let object: *mut Endpoint = handleLookup(source, token, rights)
    if object == null return -ERRNO_EPIPE
    // Management is bound to the bootstrap owner. No ownership transfer yet.
    if rights & RIGHT_MANAGE != 0 &&
        (sourceOwner != object.manager || targetOwner != object.manager) return -ERRNO_EPERM
    if object.mode == ENDPOINT_SERVICE && rights & RIGHT_RECEIVE != 0 &&
        (sourceOwner != object.manager || targetOwner != object.manager) return -ERRNO_EPERM
    return handleInstall(target, object, rights)
}

let endpointDestroy(table: *mut HandleTable, token: UWord, owner: UWord): Word {
    let entry: *mut Handle = handleEntry(table, token)
    if entry == null return -ERRNO_EBADF
    if entry.rights & RIGHT_MANAGE == 0 return -ERRNO_EPERM
    let object: *mut Endpoint = handleLookup(table, token, RIGHT_MANAGE)
    if object == null return -ERRNO_EPIPE
    if object.manager != owner return -ERRNO_EPERM
    object.state = ENDPOINT_DESTROYED
    ipcCancelEndpoint(object)
    return 0
}

// Bootstrap is the sole root of authority. Seal before the first user entry.
let endpointBootstrapMode(table: *mut HandleTable, owner: UWord, mode: UWord): Word {
    objectAssertAtomic()
    if bootstrapSealed || owner == 0 return -ERRNO_EPERM
    for i: UWord in 0..MAX_ENDPOINTS {
        let object: *mut Endpoint = &mut endpoints[i]
        if object.state != ENDPOINT_EMPTY continue
        if object.generation == WORD_MASK {
            object.state = ENDPOINT_RETIRED
            continue
        }
        object.id = i + 1
        object.generation += 1
        object.state = ENDPOINT_LIVE
        object.references = 0
        object.manager = owner
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
        let token: Word = handleInstall(table, object, RIGHT_ALL)
        if token < 0 endpointRelease(object)
        return token
    }
    return -ERRNO_ENFILE
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
    // Manager death revokes peer handles even if its manage handle was closed.
    for i: UWord in 0..MAX_ENDPOINTS {
        if endpoints[i].state == ENDPOINT_LIVE && endpoints[i].manager == owner {
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
    ENDPOINT_RAW, ENDPOINT_SERVICE,
    HANDLE_GENERATION_MAX, ENDPOINT_WAIT_CAPACITY, handleEntry, endpointRelease, handleLookup, handleClose, handleCopy, endpointDestroy,
    endpointBootstrap, endpointBootstrapService, endpointSealBootstrap, handlesReleaseTask, objectAssertAtomic }

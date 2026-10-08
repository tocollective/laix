// Building blocks of a session: a child, the endpoint it serves, its device
// grants and start record, the handles it is given, and the supervision of the
// finished graph. Every step records the first failure instead of returning
// early, so a session reads as the straight line of its wiring and checks once.
import { TaskEvent } from "../../src/task/runtime_start.m"
import { ENDPOINT_MODE_SERVICE, RIGHT_SEND, RIGHT_RECEIVE, TASK_EVENT_RECLAIMED } from "../../src/arch/wrm081632/defs.m"
import { createTask, createEndpoint, grantTaskDevices, grantDeviceExtent, grantTaskHandles,
    grantTaskImages, startTaskService, configureTask, publishTask, terminateTask, inspectTask,
    sleep } from "../syscalls.m"

let SESSION_MEMBERS: UWord = 8
let TASK_DEAD: UWord = 3

// A child under construction, and the endpoint it serves when it serves one.
// `root` is init's own handle on that endpoint; `irq` the token its device grant
// returned (zero when the grant has none).
type Service {
    reference: UWord,
    root: UWord,
    irq: UWord,
    oneShot: Bool, // runs to completion and ends: its exit is not a failure
}
let mut members: Service[8]
let mut memberCount: UWord
let mut sessionError: Word
let mut listWords: UWord[12]
let mut listCount: UWord
let mut sessionEvent: TaskEvent

let sessionReset(): Void {
    for i: UWord in 0..SESSION_MEMBERS {
        members[i].reference = 0
        members[i].root = 0
        members[i].irq = 0
        members[i].oneShot = false
    }
    memberCount = 0
    sessionError = 0
    listCount = 0
}

// True while every step so far has succeeded; the first failure is kept.
let latch(result: Word): Bool {
    if result < 0 && sessionError == 0 sessionError = result
    return sessionError == 0
}

// Records a result without needing the answer.
let note(result: Word): Void {
    latch(result)
}

// The next member and its endpoint, or null once a step has failed.
let svcSpawn(image: UWord, serves: Bool): *mut Service {
    if sessionError != 0 || memberCount == SESSION_MEMBERS {
        note(-1)
        return null
    }
    let child: Word = createTask(image)
    if !latch(child) return null
    let service: *mut Service = &mut members[memberCount]
    memberCount += 1
    service.reference = child as UWord
    service.root = 0
    service.irq = 0
    service.oneShot = false
    if serves {
        let root: Word = createEndpoint(ENDPOINT_MODE_SERVICE, child as UWord)
        if !latch(root) return null
        service.root = root as UWord
    }
    return service
}

// A program that does its work and exits, such as an acceptance client.
let svcOneShot(service: *mut Service): Void {
    if service != null service.oneShot = true
}

let svcDevices(service: *mut Service, devices: UWord): Void {
    if service == null || sessionError != 0 return
    let token: Word = grantTaskDevices(service.reference, devices)
    if latch(token) service.irq = token as UWord
}

// The whole approved storage root as the child's extent (zero bytes mean the rest).
let svcExtent(service: *mut Service, flags: UWord): Void {
    if service == null || sessionError != 0 return
    note(grantDeviceExtent(service.reference, 0, 0, flags))
}

// A server's checked start record: its own receive handle, the send handle to the
// service below it (or null), the interrupt token from its device grant.
let svcServe(service: *mut Service, role: UWord, protocol: UWord, below: *Service): Void {
    if service == null || sessionError != 0 return
    let mut upstream: UWord = 0
    if below != null upstream = below.root
    note(startTaskService(service.reference, role, protocol, service.root, upstream, service.irq))
}

// A client's checked start record: a send handle to `via`, and one to `second`.
let svcClient(service: *mut Service, role: UWord, protocol: UWord, via: *Service, second: *Service): Void {
    if service == null || via == null || sessionError != 0 return
    let mut upstream: UWord = 0
    if second != null upstream = second.root
    note(startTaskService(service.reference, role, protocol, via.root, upstream, 0))
}

// Runtime start record: a server's own receive handle.
let svcReceive(service: *mut Service): Void {
    if service == null || sessionError != 0 return
    note(configureTask(service.reference, service.root, RIGHT_RECEIVE, 0))
}

// Runtime start record with one send handle (a program that talks to one service).
let svcSendTo(service: *mut Service, via: *Service): Void {
    if service == null || via == null || sessionError != 0 return
    note(configureTask(service.reference, via.root, RIGHT_SEND, 0))
}

// Runtime start record with no endpoint; the handles come in the start list.
let svcBare(service: *mut Service): Void {
    if service == null || sessionError != 0 return
    note(configureTask(service.reference, 0, 0, 0))
}

let listBegin(): Void { listCount = 0 }

// Appends a send handle to `via`.
let listSend(via: *Service): Void {
    if via == null || listCount == 6 {
        note(-1)
        return
    }
    listWords[2 * listCount] = via.root
    listWords[2 * listCount + 1] = RIGHT_SEND
    listCount += 1
}

// Appends a plain word, such as the interrupt token of a device grant.
let listWord(value: UWord): Void {
    if listCount == 6 {
        note(-1)
        return
    }
    listWords[2 * listCount] = value
    listWords[2 * listCount + 1] = 0
    listCount += 1
}

let svcList(service: *mut Service): Void {
    if service == null || sessionError != 0 return
    note(grantTaskHandles(service.reference, &listWords[0], listCount))
}

let svcImages(service: *mut Service, images: UWord): Void {
    if service == null || sessionError != 0 return
    note(grantTaskImages(service.reference, images))
}

// Publishes every member in creation order: servers are spawned before their clients.
let sessionPublish(): Word {
    for i: UWord in 0..memberCount {
        if sessionError != 0 break
        note(publishTask(members[i].reference))
    }
    return sessionError
}

// Stops everything the session created. Unpublished children are discarded with
// their resources, running ones are terminated.
let sessionTeardown(): Void {
    let mut i: UWord = memberCount
    while i != 0 {
        i -= 1
        if members[i].reference != 0 terminateTask(members[i].reference, -1)
        members[i].reference = 0
    }
    memberCount = 0
}

// Supervision: the session is one unit. When a member that should keep running
// ends, the rest is stopped and init reports the member's index. One-shot members
// may end. Restart policy per service belongs here.
let sessionSupervise(): Word {
    while true {
        for i: UWord in 0..memberCount {
            let ended: Word = (10 + i) as Word
            if inspectTask(members[i].reference, &mut sessionEvent) != 0 return ended
            let finished: Bool = sessionEvent.state == TASK_DEAD || sessionEvent.flags & TASK_EVENT_RECLAIMED != 0
            if finished && !members[i].oneShot return ended
        }
        if sleep(1) != 0 return 30
    }
    return 0
}
export { Service, sessionReset, latch, svcSpawn, svcOneShot, svcDevices, svcExtent, svcServe, svcClient, svcReceive,
    svcSendTo, svcBare, listBegin, listSend, listWord, svcList, svcImages, sessionPublish,
    sessionTeardown, sessionSupervise, sessionError }

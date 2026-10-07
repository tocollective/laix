// G4 CPU scenario: a supervisor replaces a client before its reply namespace
// is exhausted. The probe seeds the client's namespace near its limit at
// lifetimeArmed (user mode cannot, and the kernel never resets counters). The
// supervisor answers the client's calls, reads the remaining lifetime until the
// client is due, drains and collects it, and constructs a replacement that must
// land in a different namespace. The client treats EOVERFLOW as a failure.
import { RuntimeStart, TaskEvent } from "../../../src/task/runtime_start.m"
import { lifetimeDue, retireClient } from "../../../user/recovery/policy.m"
import { createTask, configureTask, publishTask, createEndpoint, closeHandle,
    callTimed, tryAccept, reply, inspectTask, yield, exit, AcceptResult } from "../../../user/syscalls.m"
import { ENDPOINT_MODE_SERVICE, RIGHT_SEND, ERRNO_EOVERFLOW,
    LIFETIME_REPLY_RESERVE } from "../../../src/arch/wrm081632/defs.m"
extern let lifetimeArmed(): Void
extern let lifetimeReplaced(): Void
extern let lifetimeDone(): Void
let mut request: UWord[8]
let mut response: UWord[8]
let mut event: TaskEvent
let mut accepted: AcceptResult
let expect(ok: Bool, code: Word): Void { if !ok exit(code) }

// Back-to-back calls on the control endpoint. Running out of namespace before
// the supervisor replaces this task is the failure the scenario must prevent.
let lifetimeClient(start: *RuntimeStart): Void {
    let mut count: UWord = 0
    while true {
        request[0] = count
        let size: Word = callTimed(start.endpoint, &request[0] as *UByte, 4, &mut response[0] as *mut UByte, 32, 5)
        expect(size != -ERRNO_EOVERFLOW, 60)
        expect(size == 4 && response[0] == count, 61)
        count += 1
    }
}

// Answers at most one pending call and returns how many it answered (0 or 1).
let serveOne(control: UWord): UWord {
    let size: Word = tryAccept(control, &mut request[0] as *mut UByte, 8, &mut accepted)
    if size < 0 return 0
    let sent: Word = reply(accepted.replyToken, &request[0] as *UByte, size as UWord)
    expect(sent <= 32, 5)
    return 1
}

let lifetimePolicyMain(start: *RuntimeStart, bytes: UWord): Void {
    if start.argument == 1 lifetimeClient(start)
    let control: Word = createEndpoint(ENDPOINT_MODE_SERVICE, 0)
    expect(control > 0, 1)
    let client: Word = createTask(5)
    expect(client > 0 && configureTask(client as UWord, control as UWord, RIGHT_SEND, 1) == 0, 2)
    // The client is unpublished: it cannot have made a call yet.
    lifetimeArmed()
    expect(publishTask(client as UWord) == 0, 3)

    let mut served: UWord = 0
    let mut due: Word = 0
    for spin: UWord in 0..200000 {
        served += serveOne(control as UWord)
        due = lifetimeDue(client as UWord, LIFETIME_REPLY_RESERVE)
        expect(due >= 0, 4)
        if due == 1 break
        expect(yield() == 0, 6)
    }
    expect(due == 1 && served > 0, 7)

    // Drain, reclaim and collect before the client can see EOVERFLOW, then replace.
    expect(retireClient(client as UWord) == 0, 8)
    expect(inspectTask(client as UWord, &mut event) != 0, 9)
    let replacement: Word = createTask(5)
    expect(replacement > 0 && (replacement as UWord & 255) != (client as UWord & 255), 10)
    expect(configureTask(replacement as UWord, control as UWord, RIGHT_SEND, 1) == 0 &&
        publishTask(replacement as UWord) == 0, 11)
    expect(lifetimeDue(replacement as UWord, LIFETIME_REPLY_RESERVE) == 0, 12)
    lifetimeReplaced()

    // The replacement works from its own, fresh namespace.
    let mut answered: UWord = 0
    for spin: UWord in 0..200000 {
        answered += serveOne(control as UWord)
        if answered >= 20 break
        expect(yield() == 0, 13)
    }
    expect(answered >= 20 && lifetimeDue(replacement as UWord, LIFETIME_REPLY_RESERVE) == 0, 14)
    expect(retireClient(replacement as UWord) == 0, 15)
    expect(closeHandle(control as UWord) == 0, 16)
    lifetimeDone()
    exit(0)
}
export { lifetimePolicyMain }

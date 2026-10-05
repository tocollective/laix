// Isolated three-task sharing CPU fixture; startup argument chooses death order.
import { kernelInit, kernelBootInfo } from "../../../src/kernel/boot.m"
import { taskCreateProgram } from "../../../src/task/program.m"
import { taskGet, Task, taskPublish, taskStart } from "../../../src/task/task.m"
import { taskInstallRuntimeStart, taskControlBootstrap } from "../../../src/task/control.m"
import { endpointBootstrap, handleCopy } from "../../../src/ipc/objects.m"
import { RIGHT_SEND, RIGHT_RECEIVE, TASK_RIGHT_INSPECT, TASK_RIGHT_COLLECT } from "../../../src/arch/wrm081632/defs.m"
import { panic, setPanicStage } from "../../../src/kernel/panic.m"
extern let sharingImageStart: UByte
extern let sharingImageEnd: UByte
let fixtureEndpoint(observer: *mut Task): UWord {
    let result: Word = endpointBootstrap(&mut observer.handles, observer.id)
    if result <= 0 panic("sharing fixture endpoint failed", null)
    return result as UWord
}
let fixtureCopy(observer: *mut Task, token: UWord, target: *mut Task): UWord {
    let result: Word = handleCopy(&mut observer.handles, token, &mut target.handles,
        observer.id, target.id, RIGHT_SEND | RIGHT_RECEIVE)
    if result <= 0 panic("sharing fixture handle copy failed", null)
    return result as UWord
}
let main(): Word {
    kernelInit()
    setPanicStage("memory-sharing-prepare")
    for i: UWord in 0..3 {
        if taskCreateProgram(&sharingImageStart as UWord, &sharingImageEnd as UWord) != i + 1 {
            panic("sharing fixture image allocation failed", null)
        }
    }
    let owner: *mut Task = taskGet(1)
    let borrower: *mut Task = taskGet(2)
    let observer: *mut Task = taskGet(3)
    let exchange: UWord = fixtureEndpoint(observer)
    let notification: UWord = fixtureEndpoint(observer)
    let ownerGate: UWord = fixtureEndpoint(observer)
    let borrowerGate: UWord = fixtureEndpoint(observer)
    let ownerData: *mut UWord = owner.pages[1] as *mut UWord
    let borrowerData: *mut UWord = borrower.pages[1] as *mut UWord
    let observerData: *mut UWord = observer.pages[1] as *mut UWord
    ownerData[0] = fixtureCopy(observer, notification, owner)
    ownerData[1] = fixtureCopy(observer, ownerGate, owner)
    borrowerData[0] = fixtureCopy(observer, notification, borrower)
    borrowerData[1] = fixtureCopy(observer, borrowerGate, borrower)
    observerData[0] = ownerGate
    observerData[1] = borrowerGate
    if !taskInstallRuntimeStart(1, fixtureCopy(observer, exchange, owner), RIGHT_SEND | RIGHT_RECEIVE, 0) ||
        !taskInstallRuntimeStart(2, fixtureCopy(observer, exchange, borrower), RIGHT_SEND | RIGHT_RECEIVE, 1) ||
        !taskInstallRuntimeStart(3, notification, RIGHT_SEND | RIGHT_RECEIVE, 2) ||
        !taskControlBootstrap(3, 1, TASK_RIGHT_INSPECT | TASK_RIGHT_COLLECT) ||
        !taskControlBootstrap(3, 2, TASK_RIGHT_INSPECT | TASK_RIGHT_COLLECT) panic("sharing fixture startup failed", null)
    owner.reusable = true
    observer.createImages = 1
    for i: UWord in 1..4 { if !taskPublish(i) panic("sharing fixture publication failed", null) }
    setPanicStage("memory-sharing-user")
    taskStart(kernelBootInfo.clock)
    return 1
}

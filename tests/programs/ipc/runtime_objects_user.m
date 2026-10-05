// Runtime object CPU acceptance: factories, transport, quotas and device renewal.
import { RuntimeStart, TaskEvent } from "../../../src/task/runtime_start.m"
import { createEndpoint, destroyEndpoint, closeHandle, copyHandle, send, recv,
    call, accept, reply, AcceptResult, createTask, grantTaskDevices,
    configureTask, publishTask, terminateTask, collectTask, yield, exit } from "../../../user/syscalls.m"
import { ENDPOINT_MODE_RAW, ENDPOINT_MODE_SERVICE, RIGHT_SEND, RIGHT_RECEIVE,
    RIGHT_ALL, DEVICE_INPUT, DEVICE_UART_TX, ERRNO_EPERM, ERRNO_ENFILE,
    ERRNO_EBADF, ERRNO_EAGAIN, RUNTIME_START_MAGIC, RUNTIME_START_BYTES } from "../../../src/arch/wrm081632/defs.m"
let mut message: UWord
let mut accepted: AcceptResult
let mut event: TaskEvent
let expect(value: Bool, code: Word): Void { if !value exit(code) }
let exchange(control: UWord, endpoint: UWord): Void {
    message = endpoint
    expect(send(control, (&message) as *UByte, 4) == 4, 10)
}
let objectsUserMain(start: *RuntimeStart, bytes: UWord): Void {
    expect(bytes == RUNTIME_START_BYTES && start.magic == RUNTIME_START_MAGIC, 1)
    if start.argument == 1 {
        expect(createEndpoint(ENDPOINT_MODE_RAW, 0) == -ERRNO_EPERM, 2)
        for mode: UWord in ENDPOINT_MODE_RAW..(ENDPOINT_MODE_SERVICE + 1) {
            for i: UWord in 0..20 {
                expect(recv(start.endpoint, (&mut message) as *mut UByte, 4) == 4, 3)
                let token: UWord = message
                expect(copyHandle(token, start.reference, RIGHT_ALL) == -ERRNO_EPERM, 4)
                if mode == ENDPOINT_MODE_RAW {
                    expect(recv(token, (&mut message) as *mut UByte, 4) == 4 && message == i, 5)
                    expect(send(token, (&message) as *UByte, 4) == 4, 6)
                } else {
                    message = i
                    expect(call(token, (&message) as *UByte, 4, (&mut message) as *mut UByte, 4) == 4, 7)
                    expect(message == i + 1, 8)
                }
                expect(closeHandle(token) == 0, 9)
                expect(send(start.endpoint, (&message) as *UByte, 4) == 4, 11)
            }
        }
        exit(0)
    }
    let mut stale: UWord = 0
    for mode: UWord in ENDPOINT_MODE_RAW..(ENDPOINT_MODE_SERVICE + 1) {
        for i: UWord in 0..20 {
            let root: Word = createEndpoint(mode, 0)
            expect(root > 0, 12)
            if stale != 0 expect(destroyEndpoint(stale) == -ERRNO_EBADF, 13)
            let mut rights: UWord = RIGHT_SEND
            if mode == ENDPOINT_MODE_RAW rights |= RIGHT_RECEIVE
            let peer: Word = copyHandle(root as UWord, 2, rights)
            expect(peer > 0, 14)
            exchange(start.endpoint, peer as UWord)
            if mode == ENDPOINT_MODE_RAW {
                message = i
                expect(send(root as UWord, (&message) as *UByte, 4) == 4, 15)
                expect(recv(root as UWord, (&mut message) as *mut UByte, 4) == 4 && message == i, 16)
            } else {
                expect(accept(root as UWord, (&mut message) as *mut UByte, 4, &mut accepted) == 4, 17)
                expect(message == i, 18)
                message += 1
                expect(reply(accepted.replyToken, (&message) as *UByte, 4) == 4, 19)
            }
            expect(recv(start.endpoint, (&mut message) as *mut UByte, 4) == 4, 20)
            expect(destroyEndpoint(root as UWord) == 0 && closeHandle(root as UWord) == 0, 21)
            stale = root as UWord
            let mut busy: UWord = 0
            while busy < 5000 busy += 1
        }
    }
    // The persistent boot control endpoint consumes one of the twelve charges.
    let mut held: UWord[11]
    for i: UWord in 0..11 {
        let token: Word = createEndpoint(ENDPOINT_MODE_RAW, 0)
        expect(token > 0, 22)
        held[i] = token as UWord
    }
    expect(createEndpoint(ENDPOINT_MODE_RAW, 0) == -ERRNO_ENFILE, 23)
    for i: UWord in 0..11 expect(closeHandle(held[i]) == 0, 24)
    let recovered: Word = createEndpoint(ENDPOINT_MODE_RAW, 0)
    expect(recovered > 0 && closeHandle(recovered as UWord) == 0, 25)
    // Renew a masked, exclusive broker grant without publishing partial children.
    let mut oldIrq: UWord = 0
    for i: UWord in 0..2 {
        let child: Word = createTask(1)
        expect(child > 0, 26)
        let irq: Word = grantTaskDevices(child as UWord, DEVICE_INPUT)
        expect(irq > 0 && (irq as UWord) != oldIrq, 27)
        oldIrq = irq as UWord
        expect(terminateTask(child as UWord, 0) == 0, 28)
    }
    let child: Word = createTask(1)
    expect(child > 0 && grantTaskDevices(child as UWord, DEVICE_UART_TX) == 0, 29)
    expect(configureTask(child as UWord, 0, 0, 0) == 0 && publishTask(child as UWord) == 0, 30)
    while true {
        let result: Word = collectTask(child as UWord, &mut event)
        if result == 0 break
        expect(result == -ERRNO_EAGAIN, 31)
        expect(yield() == 0, 33)
    }
    expect(event.code == 0, 32)
    exit(0)
}
export { objectsUserMain }

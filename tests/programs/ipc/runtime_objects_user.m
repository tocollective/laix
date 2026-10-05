// Runtime object CPU acceptance: factories, transport, quotas and device renewal.
import { TransferResult, reserveTransfer, commitTransfer, cancelTransfer, collectTransfer, sleep } from "../../../user/syscalls.m"
import { ERRNO_EMFILE, ERRNO_EPIPE } from "../../../src/arch/wrm081632/defs.m"
import { RuntimeStart, TaskEvent } from "../../../src/task/runtime_start.m"
import { createEndpoint, destroyEndpoint, closeHandle, copyHandle, send, recv,
    call, accept, reply, AcceptResult, createTask, grantTaskDevices,
    configureTask, publishTask, terminateTask, collectTask, yield, exit } from "../../../user/syscalls.m"
import { ENDPOINT_MODE_RAW, ENDPOINT_MODE_SERVICE, RIGHT_SEND, RIGHT_RECEIVE,
    RIGHT_ALL, DEVICE_INPUT, DEVICE_UART_TX, ERRNO_EPERM, ERRNO_ENFILE,
    ERRNO_EBADF, ERRNO_EAGAIN, RUNTIME_START_MAGIC, RUNTIME_START_BYTES } from "../../../src/arch/wrm081632/defs.m"
let mut message: UWord
let mut accepted: AcceptResult
let mut transfer: TransferResult
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
        // Cancelled and expired permissions never install even valid authority.
        let cancelled: Word = reserveTransfer(2, 1, RIGHT_SEND, 1)
        expect(cancelled > 0 && cancelTransfer(cancelled as UWord) == 0, 40)
        exchange(start.endpoint, cancelled as UWord)
        let expired: Word = reserveTransfer(2, 1, RIGHT_SEND, 1)
        expect(expired > 0, 41)
        exchange(start.endpoint, expired as UWord)
        expect(sleep(2) == 0, 42)
        expect(recv(start.endpoint, (&mut message) as *mut UByte, 4) == 4, 43)
        expect(collectTransfer(expired as UWord, &mut transfer) == -ERRNO_EBADF &&
            transfer.sender == 0 && transfer.rights == 0, 44)
        for mode: UWord in ENDPOINT_MODE_RAW..(ENDPOINT_MODE_SERVICE + 1) {
            for i: UWord in 0..20 {
                let mut rights: UWord = RIGHT_SEND
                if mode == ENDPOINT_MODE_RAW rights |= RIGHT_RECEIVE
                let ticket: Word = reserveTransfer(2, 1, rights, 60)
                expect(ticket > 0, 45)
                exchange(start.endpoint, ticket as UWord)
                expect(recv(start.endpoint, (&mut message) as *mut UByte, 4) == 4, 3)
                expect(collectTransfer(ticket as UWord, &mut transfer) > 0 &&
                    transfer.sender == 1 && transfer.rights == rights &&
                    (transfer.handle as UWord) & 255 == 2, 46)
                let token: UWord = transfer.handle as UWord
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
        // Exhaust only this table, then restore it; supervisor keeps running.
        let mut copies: UWord[15]
        for i: UWord in 0..15 {
            let copy: Word = copyHandle(start.endpoint, start.reference, RIGHT_SEND)
            expect(copy > 0, 47)
            copies[i] = copy as UWord
        }
        expect(copyHandle(start.endpoint, start.reference, RIGHT_SEND) == -ERRNO_EMFILE, 48)
        expect(reserveTransfer(2, 1, RIGHT_SEND, 1) == -ERRNO_EMFILE, 49)
        for i: UWord in 0..15 expect(closeHandle(copies[i]) == 0, 50)
        // Sender death after commit revokes its object; authenticated metadata
        // survives until collected, and the stale reference must still close.
        let finalTicket: Word = reserveTransfer(2, 1, RIGHT_SEND, 60)
        expect(finalTicket > 0, 51)
        exchange(start.endpoint, finalTicket as UWord)
        expect(recv(start.endpoint, (&mut message) as *mut UByte, 4) == -ERRNO_EPIPE, 52)
        expect(collectTransfer(finalTicket as UWord, &mut transfer) > 0 && transfer.sender == 1, 53)
        expect(send(transfer.handle as UWord, (&message) as *UByte, 4) == -ERRNO_EPIPE, 54)
        expect(closeHandle(transfer.handle as UWord) == 0, 55)
        exit(0)
    }
    expect(recv(start.endpoint, (&mut message) as *mut UByte, 4) == 4, 56)
    expect(commitTransfer(start.endpoint, 2, message, RIGHT_SEND) == -ERRNO_EBADF, 57)
    expect(recv(start.endpoint, (&mut message) as *mut UByte, 4) == 4, 58)
    let expired: UWord = message
    expect(sleep(2) == 0, 59)
    expect(commitTransfer(start.endpoint, 2, expired, RIGHT_SEND) == -ERRNO_EBADF, 60)
    exchange(start.endpoint, 0)
    expect(reserveTransfer(15, 2, RIGHT_SEND, 1) == -ERRNO_EPERM, 61)
    let mut stale: UWord = 0
    for mode: UWord in ENDPOINT_MODE_RAW..(ENDPOINT_MODE_SERVICE + 1) {
        for i: UWord in 0..20 {
            let root: Word = createEndpoint(mode, 0)
            expect(root > 0, 12)
            if stale != 0 expect(destroyEndpoint(stale) == -ERRNO_EBADF, 13)
            let mut rights: UWord = RIGHT_SEND
            if mode == ENDPOINT_MODE_RAW rights |= RIGHT_RECEIVE
            expect(recv(start.endpoint, (&mut message) as *mut UByte, 4) == 4, 14)
            let ticket: UWord = message
            for attempt: UWord in 0..16 expect(copyHandle(root as UWord, 2, RIGHT_SEND) == -ERRNO_EPERM, 62)
            expect(commitTransfer(root as UWord, 2, ticket + 256, rights) == -ERRNO_EBADF, 63)
            expect(commitTransfer(root as UWord, 2, ticket, RIGHT_ALL) == -ERRNO_EPERM, 64)
            expect(commitTransfer(root as UWord, 2, ticket, rights) == 0, 65)
            expect(commitTransfer(root as UWord, 2, ticket, rights) == -ERRNO_EBADF, 66)
            exchange(start.endpoint, 0)
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
    expect(recv(start.endpoint, (&mut message) as *mut UByte, 4) == 4, 67)
    expect(commitTransfer(start.endpoint, 2, message, RIGHT_SEND) == 0, 68)
    exit(0)
}
export { objectsUserMain }

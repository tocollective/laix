// Ethernet driver service (docs/NETWORK.md). It owns the card through the kernel
// broker and gives its one client whole frames, 16 bytes per IPC message, through
// one send and one receive staging buffer. It parses nothing: addresses,
// protocols and policy belong to the stack above.
//
// Start: the endpoint is its receive handle; the start handle list holds the
// interrupt token. Requests are 16 bytes unless noted:
//   INFO            MAC and link: words 3 (bytes 0-3), 4 (bytes 4-5, link in bit 16), 5 (discarded frames)
//   PUT off + data  (32 bytes) 16 bytes into the send buffer at off
//   SEND length     the buffer as one frame
//   POLL seconds    the length of the next received frame, waiting up to `seconds` for one; 0: none
//   GET off         16 bytes of the received frame at off, word 3 is how many are valid
//   DONE            discards the received frame so POLL fetches the next
import { RuntimeStart } from "../../src/task/runtime_start.m"
import { START_BLOCK_VA, RUNTIME_START_BYTES, RUNTIME_START_MAGIC, RUNTIME_START_VERSION,
    NETDRV_INFO_HEADER, NETDRV_PUT_HEADER, NETDRV_SEND_HEADER, NETDRV_POLL_HEADER, NETDRV_GET_HEADER,
    NETDRV_DONE_HEADER, NET_RESPONSE_HEADER, NET_FRAME_MIN, NET_FRAME_MAX, NET_INFO_BYTES, DATA_GENERATION,
    ERRNO_EINVAL, ERRNO_EPIPE, ERRNO_EIO, ERRNO_EAGAIN, ERRNO_ETIMEDOUT } from "../../src/arch/wrm081632/defs.m"
import { AcceptResult, accept, reply, irqWait, irqComplete, netDeviceInfo, netDeviceSend,
    netDeviceReceive, exit } from "../syscalls.m"
import { startHandle } from "../starthandles.m"

let mut netdrvRequest: UWord[8]
let mut netdrvResponse: UWord[8]
let mut netdrvSend: UWord[380]
let mut netdrvReceive: UWord[380]
let mut netdrvInfo: UWord[4]
let mut netdrvLength: UWord // bytes in the receive buffer; zero when it is empty
let mut netdrvFailed: Bool
let mut netdrvGeneration: UWord = DATA_GENERATION

// Fetches the next frame from the broker if none is held. A device failure is
// final: the service ends and its supervisor starts a replacement.
let netdrvFetch(): Word {
    if netdrvLength != 0 return netdrvLength as Word
    let got: Word = netDeviceReceive(&mut netdrvReceive[0] as *mut UByte, NET_FRAME_MAX)
    if got > 0 {
        netdrvLength = got as UWord
        return got
    }
    if got == 0 || got == -ERRNO_EAGAIN return 0
    if got == -ERRNO_EIO netdrvFailed = true
    return got
}

let netdrvHandle(request: *UWord, size: UWord, response: *mut UWord, irq: UWord): Void {
    for i: UWord in 0..8 response[i] = 0
    response[0] = NET_RESPONSE_HEADER
    response[1] = (-ERRNO_EINVAL) as UWord
    response[2] = netdrvGeneration
    let header: UWord = request[0]
    let put: Bool = header == NETDRV_PUT_HEADER && size == 32
    let plain: Bool = size == 16 && (header == NETDRV_INFO_HEADER || header == NETDRV_SEND_HEADER ||
        header == NETDRV_POLL_HEADER || header == NETDRV_GET_HEADER || header == NETDRV_DONE_HEADER)
    if !put && !plain return
    if request[1] != netdrvGeneration {
        response[1] = (-ERRNO_EPIPE) as UWord
        return
    }
    if header == NETDRV_INFO_HEADER {
        if request[2] != 0 || request[3] != 0 return
        let got: Word = netDeviceInfo(&mut netdrvInfo[0] as *mut UByte)
        if got != NET_INFO_BYTES as Word {
            response[1] = got as UWord
            if got == -ERRNO_EIO netdrvFailed = true
            return
        }
        response[1] = 0
        response[3] = netdrvInfo[0]
        response[4] = netdrvInfo[1]
        response[5] = netdrvInfo[3]
    } else if put {
        if request[3] != 0 || request[2] & 15 != 0 || request[2] > 1504 return
        for k: UWord in 0..4 netdrvSend[request[2] / 4 + k] = request[4 + k]
        response[1] = 0
    } else if header == NETDRV_SEND_HEADER {
        if request[3] != 0 || request[2] < NET_FRAME_MIN || request[2] > NET_FRAME_MAX return
        let sent: Word = netDeviceSend(&netdrvSend[0] as *UByte, request[2])
        response[1] = sent as UWord
        if sent > 0 {
            response[1] = 0
            response[3] = sent as UWord
        } else if sent == -ERRNO_EIO netdrvFailed = true
    } else if header == NETDRV_POLL_HEADER {
        if request[3] != 0 || request[2] > 60 return
        let mut got: Word = netdrvFetch()
        if got == 0 && request[2] != 0 {
            // The broker armed the interrupt when it found the ring empty.
            let waited: Word = irqWait(irq, request[2])
            if waited == 0 got = netdrvFetch()
            else if waited != -ERRNO_ETIMEDOUT {
                response[1] = waited as UWord
                return
            }
        }
        if got < 0 {
            response[1] = got as UWord
            return
        }
        response[1] = 0
        response[3] = got as UWord
    } else if header == NETDRV_GET_HEADER {
        if request[3] != 0 || request[2] & 15 != 0 || request[2] >= netdrvLength return
        let mut valid: UWord = netdrvLength - request[2]
        if valid > 16 valid = 16
        for k: UWord in 0..4 response[4 + k] = netdrvReceive[request[2] / 4 + k]
        response[1] = 0
        response[3] = valid
    } else {
        if request[2] != 0 || request[3] != 0 return
        netdrvLength = 0
        response[1] = 0
    }
}

let netdrvMain(start: *RuntimeStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != RUNTIME_START_BYTES ||
        start.magic != RUNTIME_START_MAGIC || start.version != RUNTIME_START_VERSION) exit(1)
    let irq: UWord = startHandle(start, 0)
    if start.endpoint == 0 || irq == 0 exit(2)
    if irqComplete(irq) != 0 exit(3)
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(start.endpoint, &mut netdrvRequest[0] as *mut UByte, 32, &mut accepted)
        if size < 0 exit(4)
        netdrvHandle(&netdrvRequest[0], size as UWord, &mut netdrvResponse[0], irq)
        let sent: Word = reply(accepted.replyToken, &netdrvResponse[0] as *UByte, 32)
        if sent > 32 exit(5)
        if netdrvFailed exit(6)
    }
}
export { netdrvMain, netdrvHandle }

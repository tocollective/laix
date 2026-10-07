// A minimal IPv4 stack as a service (docs/NETWORK.md): Ethernet, ARP, IPv4 without
// fragments, ICMP echo (it pings, and answers pings) and UDP, enough for name
// resolution (DNS over UDP) and ping. No TCP. It is a pull-model stack: frames
// are read and answered while a request waits for its reply, never in the
// background. The address is the emulated network's fixed one.
//
// Start: the endpoint is its receive handle; the start handle list holds [0] the
// Ethernet driver service (send). Requests (16 bytes unless noted):
//   INFO                 address, gateway, MAC and link
//   PING addr seq|secs<<16   echo request; word 3 of the reply is the TTL
//   NAME off + 16 bytes  (32 bytes) fills the 64-byte host name staging buffer
//   RESOLVE seconds      the staged name to an IPv4 address (word 3), via DNS
import { RuntimeStart } from "../../src/task/runtime_start.m"
import { START_BLOCK_VA, RUNTIME_START_BYTES, RUNTIME_START_MAGIC, RUNTIME_START_VERSION,
    NETDRV_INFO_HEADER, NETDRV_PUT_HEADER, NETDRV_SEND_HEADER, NETDRV_POLL_HEADER, NETDRV_GET_HEADER,
    NETDRV_DONE_HEADER, NET_RESPONSE_HEADER, IP_INFO_HEADER, IP_PING_HEADER, IP_NAME_HEADER, IP_RESOLVE_HEADER,
    DATA_GENERATION, NET_FRAME_MAX, ERRNO_EINVAL, ERRNO_EPIPE, ERRNO_EIO, ERRNO_ENOENT, ERRNO_EPROTO,
    ERRNO_ETIMEDOUT, ERRNO_EHOSTUNREACH, ERRNO_ENETDOWN } from "../../src/arch/wrm081632/defs.m"
import { AcceptResult, accept, reply, call, exit } from "../syscalls.m"
import { startHandle } from "../starthandles.m"
import { wordsGet, wordsPut } from "../words.m"

let IP_ADDRESS: UWord = 0x0A00020F // 10.0.2.15
let IP_GATEWAY: UWord = 0x0A000202 // 10.0.2.2
let IP_DNS: UWord = 0x0A000203 // 10.0.2.3
let IP_NETMASK: UWord = 0xFFFFFF00
let ETH_ARP: UWord = 0x0806
let ETH_IPV4: UWord = 0x0800
let PROTO_ICMP: UWord = 1
let PROTO_UDP: UWord = 17
let ECHO_IDENT: UWord = 0x4C58
let DNS_PORT: UWord = 53
let FRAME_WORDS: UWord = 380
let POLLS_MAX: UWord = 64 // frames handled while waiting for one reply

let mut ipRequest: UWord[8]
let mut ipResponse: UWord[8]
let mut ipDriverRequest: UWord[8]
let mut ipDriverResponse: UWord[8]
let mut ipTx: UWord[380]
let mut ipRx: UWord[380]
let mut ipRxLength: UWord
let mut ipDriver: UWord
let mut ipMac: UWord[2] // bytes 0-3, then 4-5 with the link flag in bit 16
let mut ipLink: Bool
let mut ipArpAddress: UWord[4]
let mut ipArpMac: UWord[8] // two words (six bytes) per entry
let mut ipArpNext: UWord
let mut ipIdent: UWord
let mut ipPort: UWord = 49152
let mut ipName: UWord[16]
let mut ipGeneration: UWord = DATA_GENERATION
// What the last handled frames left for the operation that waits.
let mut ipEchoReady: Bool
let mut ipEchoFrom: UWord
let mut ipEchoSeq: UWord
let mut ipEchoTtl: UWord
let mut ipUdpReady: Bool
let mut ipUdpFrom: UWord
let mut ipUdpSource: UWord
let mut ipUdpTarget: UWord
let mut ipUdpOffset: UWord
let mut ipUdpLength: UWord
let mut ipFailed: Bool

// ---- Bytes in word arrays ------------------------------------------------

let get16(buffer: *UWord, offset: UWord): UWord {
    return (wordsGet(buffer, offset) << 8) | wordsGet(buffer, offset + 1)
}
let get32(buffer: *UWord, offset: UWord): UWord {
    return (get16(buffer, offset) << 16) | get16(buffer, offset + 2)
}
let put16(buffer: *mut UWord, offset: UWord, value: UWord): Void {
    wordsPut(buffer, offset, (value >> 8) & 255)
    wordsPut(buffer, offset + 1, value & 255)
}
let put32(buffer: *mut UWord, offset: UWord, value: UWord): Void {
    put16(buffer, offset, value >> 16)
    put16(buffer, offset + 2, value & 0xFFFF)
}
let copyBytes(destination: *mut UWord, at: UWord, source: *UWord, start: UWord, count: UWord): Void {
    for i: UWord in 0..count wordsPut(destination, at + i, wordsGet(source, start + i))
}

// Internet checksum over bytes, accumulated and folded separately.
let sumBytes(buffer: *UWord, offset: UWord, length: UWord, start: UWord): UWord {
    let mut sum: UWord = start
    let mut i: UWord = 0
    while i + 1 < length {
        sum += get16(buffer, offset + i)
        i += 2
    }
    if i < length sum += wordsGet(buffer, offset + i) << 8
    return sum
}
let foldSum(total: UWord): UWord {
    let mut sum: UWord = total
    while sum >> 16 != 0 sum = (sum & 0xFFFF) + (sum >> 16)
    return (~sum) & 0xFFFF
}

// ---- The driver ----------------------------------------------------------

let driverCall(header: UWord, a: UWord, b: UWord, size: UWord): Word {
    ipDriverRequest[0] = header
    ipDriverRequest[1] = ipGeneration
    ipDriverRequest[2] = a
    ipDriverRequest[3] = b
    let got: Word = call(ipDriver, &ipDriverRequest[0] as *UByte, size, &mut ipDriverResponse[0] as *mut UByte, 32)
    if got < 0 {
        ipFailed = true
        return got
    }
    if got != 32 || ipDriverResponse[0] != NET_RESPONSE_HEADER || ipDriverResponse[2] != ipGeneration return -ERRNO_EPROTO
    let status: Word = ipDriverResponse[1] as Word
    if status > 0 || (status < 0 && ipDriverResponse[3] != 0) return -ERRNO_EPROTO
    if status == -ERRNO_EIO ipFailed = true
    return status
}

// Hands the frame in ipTx to the driver, 16 bytes at a time, and sends it.
let transmit(length: UWord): Word {
    let mut padded: UWord = length
    if length < 60 {
        // Frames shorter than the Ethernet minimum are padded with zeros.
        for i: UWord in length..60 wordsPut(&mut ipTx[0], i, 0)
        padded = 60
    }
    let mut offset: UWord = 0
    while offset < padded {
        ipDriverRequest[0] = NETDRV_PUT_HEADER
        ipDriverRequest[1] = ipGeneration
        ipDriverRequest[2] = offset
        ipDriverRequest[3] = 0
        for k: UWord in 0..4 ipDriverRequest[4 + k] = ipTx[offset / 4 + k]
        let got: Word = call(ipDriver, &ipDriverRequest[0] as *UByte, 32, &mut ipDriverResponse[0] as *mut UByte, 32)
        if got < 0 {
            ipFailed = true
            return got
        }
        if got != 32 || ipDriverResponse[0] != NET_RESPONSE_HEADER || ipDriverResponse[1] != 0 return -ERRNO_EPROTO
        offset += 16
    }
    return driverCall(NETDRV_SEND_HEADER, padded, 0, 16)
}

// Waits up to `seconds` for a frame and copies it into ipRx. 1: a frame, 0: none.
let receiveFrame(seconds: UWord): Word {
    let polled: Word = driverCall(NETDRV_POLL_HEADER, seconds, 0, 16)
    if polled != 0 return polled
    let length: UWord = ipDriverResponse[3]
    if length == 0 return 0
    if length > NET_FRAME_MAX return -ERRNO_EPROTO
    let mut offset: UWord = 0
    while offset < length {
        let got: Word = driverCall(NETDRV_GET_HEADER, offset, 0, 16)
        if got != 0 return got
        for k: UWord in 0..4 ipRx[offset / 4 + k] = ipDriverResponse[4 + k]
        offset += 16
    }
    ipRxLength = length
    return driverCall(NETDRV_DONE_HEADER, 0, 0, 16) + 1
}

// ---- Ethernet, ARP ---------------------------------------------------------

let ethernetHeader(destination: *UWord, destinationOffset: UWord, broadcast: Bool, ethertype: UWord): Void {
    for i: UWord in 0..6 {
        if broadcast wordsPut(&mut ipTx[0], i, 255)
        else wordsPut(&mut ipTx[0], i, wordsGet(destination, destinationOffset + i))
    }
    copyBytes(&mut ipTx[0], 6, &ipMac[0], 0, 6)
    put16(&mut ipTx[0], 12, ethertype)
}

let arpPacket(operation: UWord, targetMac: *UWord, targetOffset: UWord, targetIp: UWord, broadcast: Bool): Word {
    ethernetHeader(targetMac, targetOffset, broadcast, ETH_ARP)
    put16(&mut ipTx[0], 14, 1) // Ethernet
    put16(&mut ipTx[0], 16, ETH_IPV4)
    wordsPut(&mut ipTx[0], 18, 6)
    wordsPut(&mut ipTx[0], 19, 4)
    put16(&mut ipTx[0], 20, operation)
    copyBytes(&mut ipTx[0], 22, &ipMac[0], 0, 6)
    put32(&mut ipTx[0], 28, IP_ADDRESS)
    if broadcast {
        for i: UWord in 0..6 wordsPut(&mut ipTx[0], 32 + i, 0)
    } else copyBytes(&mut ipTx[0], 32, targetMac, targetOffset, 6)
    put32(&mut ipTx[0], 38, targetIp)
    return transmit(42)
}

let arpLearn(address: UWord, mac: *UWord, offset: UWord): Void {
    let mut slot: UWord = 4
    for i: UWord in 0..4 {
        if ipArpAddress[i] == address slot = i
    }
    if slot == 4 {
        slot = ipArpNext
        ipArpNext = (ipArpNext + 1) % 4
    }
    ipArpAddress[slot] = address
    for i: UWord in 0..6 wordsPut(&mut ipArpMac[0], slot * 6 + i, wordsGet(mac, offset + i))
}

let arpLookup(address: UWord): UWord {
    for i: UWord in 0..4 {
        if ipArpAddress[i] == address return i
    }
    return 4
}

// ---- Handling what arrives -------------------------------------------------

let ipv4Header(protocol: UWord, destination: UWord, payload: UWord): Void {
    wordsPut(&mut ipTx[0], 14, 0x45)
    wordsPut(&mut ipTx[0], 15, 0)
    put16(&mut ipTx[0], 16, 20 + payload)
    ipIdent += 1
    put16(&mut ipTx[0], 18, ipIdent & 0xFFFF)
    put16(&mut ipTx[0], 20, 0x4000) // don't fragment
    wordsPut(&mut ipTx[0], 22, 64)
    wordsPut(&mut ipTx[0], 23, protocol)
    put16(&mut ipTx[0], 24, 0)
    put32(&mut ipTx[0], 26, IP_ADDRESS)
    put32(&mut ipTx[0], 30, destination)
    put16(&mut ipTx[0], 24, foldSum(sumBytes(&ipTx[0], 14, 20, 0)))
}

// Answers an echo request found in ipRx (from the sender's MAC and address).
let echoReply(payloadOffset: UWord, payloadLength: UWord, source: UWord): Word {
    ethernetHeader(&ipRx[0], 6, false, ETH_IPV4)
    ipv4Header(PROTO_ICMP, source, payloadLength)
    copyBytes(&mut ipTx[0], 34, &ipRx[0], payloadOffset, payloadLength)
    wordsPut(&mut ipTx[0], 34, 0) // echo reply
    put16(&mut ipTx[0], 36, 0)
    put16(&mut ipTx[0], 36, foldSum(sumBytes(&ipTx[0], 34, payloadLength, 0)))
    return transmit(34 + payloadLength)
}

// Looks at the frame in ipRx: answers ARP and echo requests, and leaves echo
// replies and UDP datagrams for the operation that is waiting. Anything else is dropped.
let handleFrame(): Word {
    let length: UWord = ipRxLength
    if length < 42 return 0
    let ethertype: UWord = get16(&ipRx[0], 12)
    if ethertype == ETH_ARP {
        if get16(&ipRx[0], 14) != 1 || get16(&ipRx[0], 16) != ETH_IPV4 || wordsGet(&ipRx[0], 18) != 6 ||
            wordsGet(&ipRx[0], 19) != 4 return 0
        let operation: UWord = get16(&ipRx[0], 20)
        let sender: UWord = get32(&ipRx[0], 28)
        arpLearn(sender, &ipRx[0], 22)
        if operation == 1 && get32(&ipRx[0], 38) == IP_ADDRESS return arpPacket(2, &ipRx[0], 22, sender, false)
        return 0
    }
    if ethertype != ETH_IPV4 || wordsGet(&ipRx[0], 14) != 0x45 return 0
    let total: UWord = get16(&ipRx[0], 16)
    if total < 20 || total + 14 > length || get32(&ipRx[0], 30) != IP_ADDRESS return 0
    if get16(&ipRx[0], 20) & 0x3FFF != 0 return 0 // a fragment
    if foldSum(sumBytes(&ipRx[0], 14, 20, 0)) != 0 return 0
    let protocol: UWord = wordsGet(&ipRx[0], 23)
    let source: UWord = get32(&ipRx[0], 26)
    let payload: UWord = total - 20
    if protocol == PROTO_ICMP && payload >= 8 {
        if foldSum(sumBytes(&ipRx[0], 34, payload, 0)) != 0 return 0
        let kind: UWord = wordsGet(&ipRx[0], 34)
        if kind == 8 {
            arpLearn(source, &ipRx[0], 6)
            return echoReply(34, payload, source)
        }
        if kind == 0 && get16(&ipRx[0], 38) == ECHO_IDENT {
            ipEchoReady = true
            ipEchoFrom = source
            ipEchoSeq = get16(&ipRx[0], 40)
            ipEchoTtl = wordsGet(&ipRx[0], 22)
        }
        return 0
    }
    if protocol == PROTO_UDP && payload >= 8 {
        let datagram: UWord = get16(&ipRx[0], 38)
        if datagram < 8 || datagram > payload return 0
        let check: UWord = get16(&ipRx[0], 40)
        if check != 0 {
            let pseudo: UWord = (source >> 16) + (source & 0xFFFF) + (IP_ADDRESS >> 16) + (IP_ADDRESS & 0xFFFF) + PROTO_UDP + datagram
            if foldSum(sumBytes(&ipRx[0], 34, datagram, pseudo)) != 0 return 0
        }
        ipUdpReady = true
        ipUdpFrom = source
        ipUdpSource = get16(&ipRx[0], 34)
        ipUdpTarget = get16(&ipRx[0], 36)
        ipUdpOffset = 42
        ipUdpLength = datagram - 8
    }
    return 0
}

// Reads and handles one frame, waiting up to `seconds` for it. 1: handled, 0: none.
let pump(seconds: UWord): Word {
    let got: Word = receiveFrame(seconds)
    if got <= 0 return got
    let handled: Word = handleFrame()
    if handled < 0 return handled
    return 1
}

// ---- Sending ---------------------------------------------------------------

// The MAC of the next hop for an address in an ARP cache slot, asking if needed.
let nextHop(address: UWord): Word {
    let mut hop: UWord = address
    if address & IP_NETMASK != IP_ADDRESS & IP_NETMASK hop = IP_GATEWAY
    let mut slot: UWord = arpLookup(hop)
    let mut attempt: UWord = 0
    while slot == 4 && attempt < 3 {
        let sent: Word = arpPacket(1, &ipMac[0], 0, hop, true)
        if sent < 0 return sent
        let mut frames: UWord = 0
        while slot == 4 && frames < POLLS_MAX {
            let got: Word = pump(1)
            if got < 0 return got
            if got == 0 break
            frames += 1
            slot = arpLookup(hop)
        }
        attempt += 1
    }
    if slot == 4 return -ERRNO_EHOSTUNREACH
    return slot as Word
}

let ping(address: UWord, sequence: UWord, seconds: UWord, response: *mut UWord): Word {
    let hop: Word = nextHop(address)
    if hop < 0 return hop
    ethernetHeader(&ipArpMac[0], hop as UWord * 6, false, ETH_IPV4)
    ipv4Header(PROTO_ICMP, address, 40)
    wordsPut(&mut ipTx[0], 34, 8)
    wordsPut(&mut ipTx[0], 35, 0)
    put16(&mut ipTx[0], 36, 0)
    put16(&mut ipTx[0], 38, ECHO_IDENT)
    put16(&mut ipTx[0], 40, sequence & 0xFFFF)
    for i: UWord in 0..32 wordsPut(&mut ipTx[0], 42 + i, 97 + i % 23)
    put16(&mut ipTx[0], 36, foldSum(sumBytes(&ipTx[0], 34, 40, 0)))
    ipEchoReady = false
    let sent: Word = transmit(74)
    if sent < 0 return sent
    let mut idle: UWord = 0
    let mut frames: UWord = 0
    while idle < seconds && frames < POLLS_MAX {
        let got: Word = pump(1)
        if got < 0 return got
        if got == 0 idle += 1
        else frames += 1
        if ipEchoReady && ipEchoFrom == address && ipEchoSeq == sequence & 0xFFFF {
            response[3] = ipEchoTtl
            return 0
        }
        ipEchoReady = false
    }
    return -ERRNO_ETIMEDOUT
}

// ---- Name resolution -------------------------------------------------------

// The staged name is letters, digits, '-' and '.', in labels of 1 to 63 bytes.
let nameLength(): Word {
    let mut length: UWord = 0
    while length < 63 && wordsGet(&ipName[0], length) != 0 length += 1
    if length == 0 || wordsGet(&ipName[0], length) != 0 return -ERRNO_EINVAL
    if wordsGet(&ipName[0], length - 1) == 46 length -= 1 // an absolute name
    let mut label: UWord = 0
    for i: UWord in 0..length {
        let c: UWord = wordsGet(&ipName[0], i)
        if c == 46 {
            if label == 0 return -ERRNO_EINVAL
            label = 0
        } else {
            let letter: Bool = (c >= 65 && c <= 90) || (c >= 97 && c <= 122)
            if !letter && !(c >= 48 && c <= 57) && c != 45 return -ERRNO_EINVAL
            label += 1
        }
    }
    if length == 0 || label == 0 return -ERRNO_EINVAL
    return length as Word
}

// Offset past a (possibly compressed) name in a DNS message, or zero if it overruns.
let skipName(offset: UWord, end: UWord): UWord {
    let mut position: UWord = offset
    while position < end {
        let size: UWord = wordsGet(&ipRx[0], position)
        if size == 0 return position + 1
        if size & 0xC0 == 0xC0 {
            if position + 2 > end return 0
            return position + 2
        }
        if size & 0xC0 != 0 return 0
        position += size + 1
    }
    return 0
}

let resolve(seconds: UWord, response: *mut UWord): Word {
    let length: Word = nameLength()
    if length < 0 return length
    let hop: Word = nextHop(IP_DNS)
    if hop < 0 return hop
    // The query: header, one question for an A record in class IN.
    let base: UWord = 42
    ipPort += 1
    if ipPort > 65000 ipPort = 49153
    let ident: UWord = (ipPort * 7 + ipIdent) & 0xFFFF
    put16(&mut ipTx[0], base, ident)
    put16(&mut ipTx[0], base + 2, 0x0100) // recursion desired
    put16(&mut ipTx[0], base + 4, 1)
    put16(&mut ipTx[0], base + 6, 0)
    put16(&mut ipTx[0], base + 8, 0)
    put16(&mut ipTx[0], base + 10, 0)
    let mut at: UWord = base + 12
    let mut labelAt: UWord = at
    wordsPut(&mut ipTx[0], labelAt, 0)
    at += 1
    for i: UWord in 0..length as UWord {
        let c: UWord = wordsGet(&ipName[0], i)
        if c == 46 {
            labelAt = at
            wordsPut(&mut ipTx[0], labelAt, 0)
        } else {
            wordsPut(&mut ipTx[0], labelAt, wordsGet(&ipTx[0], labelAt) + 1)
            wordsPut(&mut ipTx[0], at, c)
        }
        // A '.' starts the next label's length byte where the '.' stood.
        at += 1
    }
    wordsPut(&mut ipTx[0], at, 0)
    at += 1
    put16(&mut ipTx[0], at, 1)
    put16(&mut ipTx[0], at + 2, 1)
    let query: UWord = at + 4 - base
    ethernetHeader(&ipArpMac[0], hop as UWord * 6, false, ETH_IPV4)
    ipv4Header(PROTO_UDP, IP_DNS, 8 + query)
    put16(&mut ipTx[0], 34, ipPort)
    put16(&mut ipTx[0], 36, DNS_PORT)
    put16(&mut ipTx[0], 38, 8 + query)
    put16(&mut ipTx[0], 40, 0)
    let pseudo: UWord = (IP_ADDRESS >> 16) + (IP_ADDRESS & 0xFFFF) + (IP_DNS >> 16) + (IP_DNS & 0xFFFF) + PROTO_UDP + 8 + query
    let mut check: UWord = foldSum(sumBytes(&ipTx[0], 34, 8 + query, pseudo))
    if check == 0 check = 0xFFFF
    put16(&mut ipTx[0], 40, check)
    ipUdpReady = false
    let sent: Word = transmit(42 + query)
    if sent < 0 return sent
    let mut idle: UWord = 0
    let mut frames: UWord = 0
    while idle < seconds && frames < POLLS_MAX {
        let got: Word = pump(1)
        if got < 0 return got
        if got == 0 idle += 1
        else frames += 1
        if !ipUdpReady continue
        ipUdpReady = false
        if ipUdpFrom != IP_DNS || ipUdpSource != DNS_PORT || ipUdpTarget != ipPort continue
        let end: UWord = ipUdpOffset + ipUdpLength
        if ipUdpLength < 12 || get16(&ipRx[0], ipUdpOffset) != ident continue
        let flags: UWord = get16(&ipRx[0], ipUdpOffset + 2)
        if flags & 0x8000 == 0 continue
        if flags & 15 == 3 return -ERRNO_ENOENT
        if flags & 15 != 0 return -ERRNO_EIO
        let questions: UWord = get16(&ipRx[0], ipUdpOffset + 4)
        let answers: UWord = get16(&ipRx[0], ipUdpOffset + 6)
        let mut position: UWord = ipUdpOffset + 12
        for q: UWord in 0..questions {
            position = skipName(position, end)
            if position == 0 || position + 4 > end return -ERRNO_EPROTO
            position += 4
        }
        for a: UWord in 0..answers {
            position = skipName(position, end)
            if position == 0 || position + 10 > end return -ERRNO_EPROTO
            let kind: UWord = get16(&ipRx[0], position)
            let klass: UWord = get16(&ipRx[0], position + 2)
            let size: UWord = get16(&ipRx[0], position + 8)
            position += 10
            if position + size > end return -ERRNO_EPROTO
            if kind == 1 && klass == 1 && size == 4 {
                response[3] = get32(&ipRx[0], position)
                return 0
            }
            position += size
        }
        return -ERRNO_ENOENT
    }
    return -ERRNO_ETIMEDOUT
}

// ---- The service -----------------------------------------------------------

let ipHandle(request: *UWord, size: UWord, response: *mut UWord): Void {
    for i: UWord in 0..8 response[i] = 0
    response[0] = NET_RESPONSE_HEADER
    response[1] = (-ERRNO_EINVAL) as UWord
    response[2] = ipGeneration
    let header: UWord = request[0]
    let name: Bool = header == IP_NAME_HEADER && size == 32
    let plain: Bool = size == 16 && (header == IP_INFO_HEADER || header == IP_PING_HEADER || header == IP_RESOLVE_HEADER)
    if !name && !plain return
    if request[1] != ipGeneration {
        response[1] = (-ERRNO_EPIPE) as UWord
        return
    }
    if header == IP_INFO_HEADER {
        if request[2] != 0 || request[3] != 0 return
        response[1] = 0
        response[3] = IP_ADDRESS
        response[4] = IP_GATEWAY
        response[5] = ipMac[0]
        response[6] = ipMac[1]
        if ipLink response[6] = ipMac[1] | 0x10000
    } else if name {
        if request[3] != 0 || request[2] & 15 != 0 || request[2] > 48 return
        for k: UWord in 0..4 ipName[request[2] / 4 + k] = request[4 + k]
        response[1] = 0
    } else if !ipLink {
        response[1] = (-ERRNO_ENETDOWN) as UWord
    } else if header == IP_PING_HEADER {
        let seconds: UWord = request[3] >> 16
        if seconds == 0 || seconds > 30 return
        let status: Word = ping(request[2], request[3] & 0xFFFF, seconds, response)
        response[1] = status as UWord
        if status != 0 response[3] = 0
    } else {
        if request[2] == 0 || request[2] > 30 || request[3] != 0 return
        let status: Word = resolve(request[2], response)
        response[1] = status as UWord
        if status != 0 response[3] = 0
    }
}

let ipMain(start: *RuntimeStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != RUNTIME_START_BYTES ||
        start.magic != RUNTIME_START_MAGIC || start.version != RUNTIME_START_VERSION) exit(1)
    ipDriver = startHandle(start, 0)
    if start.endpoint == 0 || ipDriver == 0 exit(2)
    let info: Word = driverCall(NETDRV_INFO_HEADER, 0, 0, 16)
    if info != 0 exit(3)
    ipMac[0] = ipDriverResponse[3]
    ipMac[1] = ipDriverResponse[4] & 0xFFFF
    ipLink = ipDriverResponse[4] >> 16 != 0
    let mut accepted: AcceptResult
    while true {
        let size: Word = accept(start.endpoint, &mut ipRequest[0] as *mut UByte, 32, &mut accepted)
        if size < 0 exit(4)
        ipHandle(&ipRequest[0], size as UWord, &mut ipResponse[0])
        let sent: Word = reply(accepted.replyToken, &ipResponse[0] as *UByte, 32)
        if sent > 32 exit(5)
        if ipFailed exit(6)
    }
}
export { ipMain, ipHandle }

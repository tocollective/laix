// Client of the IP service (docs/NETWORK.md): ping and name resolution.
import { IP_INFO_HEADER, IP_PING_HEADER, IP_NAME_HEADER, IP_RESOLVE_HEADER, NET_RESPONSE_HEADER,
    DATA_GENERATION, ERRNO_EINVAL, ERRNO_EPROTO } from "../../src/arch/wrm081632/defs.m"
import { call } from "../syscalls.m"
import { wordsPut } from "../words.m"

type IpInfo {
    address: UWord, // host order: 10.0.2.15 is 0x0A00020F
    gateway: UWord,
    macLow: UWord, // MAC bytes 0 to 3, byte 0 lowest
    macHigh: UWord, // MAC bytes 4 and 5
    link: Bool,
}

let mut ipClientRequest: UWord[8]
let mut ipClientResponse: UWord[8]

let ipClientSend(handle: UWord, header: UWord, a: UWord, b: UWord, size: UWord): Word {
    ipClientRequest[0] = header
    ipClientRequest[1] = DATA_GENERATION
    ipClientRequest[2] = a
    ipClientRequest[3] = b
    let got: Word = call(handle, &ipClientRequest[0] as *UByte, size, &mut ipClientResponse[0] as *mut UByte, 32)
    if got < 0 return got
    if got != 32 || ipClientResponse[0] != NET_RESPONSE_HEADER || ipClientResponse[2] != DATA_GENERATION return -ERRNO_EPROTO
    let status: Word = ipClientResponse[1] as Word
    if status > 0 || (status < 0 && ipClientResponse[3] != 0) return -ERRNO_EPROTO
    return status
}

let ipInfo(handle: UWord, info: *mut IpInfo): Word {
    if info == null return -ERRNO_EINVAL
    let status: Word = ipClientSend(handle, IP_INFO_HEADER, 0, 0, 16)
    if status != 0 return status
    info.address = ipClientResponse[3]
    info.gateway = ipClientResponse[4]
    info.macLow = ipClientResponse[5]
    info.macHigh = ipClientResponse[6] & 0xFFFF
    info.link = ipClientResponse[6] >> 16 != 0
    return 0
}

// One echo request. Returns the reply's time to live (1 to 255), or -ETIMEDOUT,
// or -EHOSTUNREACH when the next hop's address was not found.
let ipPing(handle: UWord, address: UWord, sequence: UWord, seconds: UWord): Word {
    let status: Word = ipClientSend(handle, IP_PING_HEADER, address, (seconds << 16) | (sequence & 0xFFFF), 16)
    if status != 0 return status
    if ipClientResponse[3] == 0 || ipClientResponse[3] > 255 return -ERRNO_EPROTO
    return ipClientResponse[3] as Word
}

// The IPv4 address (host order) of a name of at most 63 bytes, or a negative
// errno: -ENOENT no such name, -ETIMEDOUT no answer.
let ipResolve(handle: UWord, name: *UByte, seconds: UWord, address: *mut UWord): Word {
    if name == null || address == null return -ERRNO_EINVAL
    let mut length: UWord = 0
    while name[length] != 0 {
        if length == 63 return -ERRNO_EINVAL
        length += 1
    }
    if length == 0 return -ERRNO_EINVAL
    for chunk: UWord in 0..4 {
        for w: UWord in 4..8 ipClientRequest[w] = 0
        for i: UWord in 0..16 {
            let at: UWord = chunk * 16 + i
            if at < length wordsPut(&mut ipClientRequest[4], i, name[at] as UWord)
        }
        let staged: Word = ipClientSend(handle, IP_NAME_HEADER, chunk * 16, 0, 32)
        if staged != 0 return staged
        if length < chunk * 16 + 16 break
    }
    let status: Word = ipClientSend(handle, IP_RESOLVE_HEADER, seconds, 0, 16)
    if status != 0 return status
    address[0] = ipClientResponse[3]
    return 0
}
export { IpInfo, ipInfo, ipPing, ipResolve }

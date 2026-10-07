// Network acceptance client (G7). It asks the IP service for its address, pings
// the gateway twice, resolves a name that exists and one that does not, and
// prints what it found through the console, so the CPU probe can read the UART.
// Its exit code names a step that failed outright: 0 when every step behaved.
import { RuntimeStart } from "../../../src/task/runtime_start.m"
import { START_BLOCK_VA, RUNTIME_START_BYTES, RUNTIME_START_MAGIC, RUNTIME_START_VERSION,
    ERRNO_ENOENT, ERRNO_ETIMEDOUT } from "../../../src/arch/wrm081632/defs.m"
import { exit } from "../../../user/syscalls.m"
import { startHandle } from "../../../user/starthandles.m"
import { textStart, textFlush, textChar, textString, textLine, textNumber, textSigned } from "../../../user/text.m"
import { IpInfo, ipInfo, ipPing, ipResolve } from "../../../user/services/ipclient.m"

let mut ncInfo: IpInfo
let mut ncAddress: UWord[1]

let ncDotted(address: UWord): Void {
    textNumber(address >> 24, 0)
    textChar(46)
    textNumber((address >> 16) & 255, 0)
    textChar(46)
    textNumber((address >> 8) & 255, 0)
    textChar(46)
    textNumber(address & 255, 0)
}

let ncMain(ip: UWord): Word {
    let info: Word = ipInfo(ip, &mut ncInfo)
    if info != 0 return 2
    textString("address ")
    ncDotted(ncInfo.address)
    textString(" gateway ")
    ncDotted(ncInfo.gateway)
    textLine()
    textFlush()
    if !ncInfo.link {
        textString("link down")
        textLine()
        textFlush()
        return 3
    }
    for sequence: UWord in 1..3 {
        let ttl: Word = ipPing(ip, ncInfo.gateway, sequence, 3)
        textString("ping ")
        ncDotted(ncInfo.gateway)
        textString(" seq ")
        textNumber(sequence, 0)
        if ttl > 0 {
            textString(" ttl ")
            textNumber(ttl as UWord, 0)
        } else {
            textString(" failed ")
            textSigned(ttl)
        }
        textLine()
        textFlush()
        if ttl <= 0 return (10 + sequence) as Word
    }
    let found: Word = ipResolve(ip, "localhost", 5, &mut ncAddress[0])
    textString("resolve localhost: ")
    if found == 0 {
        ncDotted(ncAddress[0])
    } else if found == -ERRNO_ENOENT {
        textString("no such name")
    } else {
        textString("failed ")
        textSigned(found)
    }
    textLine()
    textFlush()
    if found != 0 && found != -ERRNO_ENOENT return 20
    let missing: Word = ipResolve(ip, "no-such-host.invalid", 5, &mut ncAddress[0])
    textString("resolve no-such-host.invalid: ")
    if missing == -ERRNO_ENOENT {
        textString("no such name")
    } else if missing == 0 {
        ncDotted(ncAddress[0])
    } else {
        textString("failed ")
        textSigned(missing)
    }
    textLine()
    textFlush()
    if missing != -ERRNO_ENOENT && missing != -ERRNO_ETIMEDOUT return 21
    textString("network ok")
    textLine()
    textFlush()
    return 0
}

let netClientMain(start: *RuntimeStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != RUNTIME_START_BYTES ||
        start.magic != RUNTIME_START_MAGIC || start.version != RUNTIME_START_VERSION) exit(1)
    let ip: UWord = startHandle(start, 0)
    let console: UWord = startHandle(start, 1)
    if ip == 0 || console == 0 exit(2)
    textStart(console)
    exit(ncMain(ip))
}
export { netClientMain, ncMain }

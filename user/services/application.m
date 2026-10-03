// Simple startup/exchange/termination acceptance application.
import { ServiceStart, serviceStartValid } from "../../src/task/service_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_CLIENT,
    START_PROTOCOL_FILE } from "../../src/arch/wrm081632/defs.m"
import { inputEvents, fileSize, fileRead } from "client.m"
import { exit } from "../syscalls.m"
let mut simpleEvents: UWord[4]
let mut simpleFlags: UWord
let mut simpleProof: UByte[16]
let mut simpleExtent: Word

let simpleMain(start: *ServiceStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != SERVICE_START_BYTES ||
        !serviceStartValid(start) || start.role != START_ROLE_CLIENT || start.protocol != START_PROTOCOL_FILE) exit(1)
    if inputEvents(start.bitmapEndpoint, &mut simpleEvents[0], &mut simpleFlags) < 0 exit(2)
    simpleExtent = fileSize(start.endpoint)
    if simpleExtent < 16 exit(3)
    if fileRead(start.endpoint, 0, &mut simpleProof[0], 16) != 16 exit(4)
    exit(0)
}
export { simpleMain }

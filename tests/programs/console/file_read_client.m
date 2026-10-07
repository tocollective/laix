// Measurement application (G1): reads the first 64 KiB of the one Files file in
// 16-byte requests, the largest the Files contract returns. The empty marker
// function gives the CPU probe a breakpoint before the timed loop; the probe ends
// the span at the client's own exit. Every read must return a full 16 bytes.
import { ServiceStart, serviceStartValid } from "../../../src/task/service_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_CLIENT } from "../../../src/arch/wrm081632/defs.m"
import { fileRead } from "../../../user/services/client.m"
import { exit } from "../../../user/syscalls.m"
let FILE_READ_BYTES: UWord = 65536
let FILE_READ_PIECE: UWord = 16
let mut fileReadPiece: UByte[16]
let mut fileReadChecksum: UWord

let fileReadArmed(): Void {
    fileReadChecksum = 0
}

let fileReadClient(start: *ServiceStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != SERVICE_START_BYTES ||
        !serviceStartValid(start) || start.role != START_ROLE_CLIENT) exit(1)
    fileReadArmed()
    let mut sum: UWord = 0
    for offset: UWord in 0..(FILE_READ_BYTES / FILE_READ_PIECE) {
        if fileRead(start.endpoint, offset * FILE_READ_PIECE, &mut fileReadPiece[0], FILE_READ_PIECE) != 16 exit(2)
        for i: UWord in 0..16 sum += fileReadPiece[i] as UWord
    }
    fileReadChecksum = sum
    exit(0)
}
export { fileReadClient, fileReadArmed }

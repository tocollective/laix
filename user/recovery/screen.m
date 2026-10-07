// Supervised Screen: the production server loop from user/screen, started from a
// RecoveryStart. Its authority arrives as the supervisor-configured endpoint, the
// dependency (send handle to this generation's bitmap storage), the video IRQ
// token and the kernel-announced font index mapping. It holds no disk right.
import { RecoveryStart } from "../../src/task/recovery_start.m"
import { screenServe } from "../screen/server.m"
import { exit } from "../syscalls.m"
import { RECOVERY_START_BYTES, RUNTIME_START_MAGIC } from "../../src/arch/wrm081632/defs.m"

let recoveryScreenMain(start: *RecoveryStart, bytes: UWord): Void {
    if bytes != RECOVERY_START_BYTES || start.magic != RUNTIME_START_MAGIC ||
        start.generation == 0 || start.reference == 0 || start.dependency == 0 ||
        start.irq == 0 || start.blob == 0 || start.blobBytes == 0 exit(1)
    screenServe(start.endpoint, start.dependency, start.irq, start.blob, start.blobBytes)
}
export { recoveryScreenMain }

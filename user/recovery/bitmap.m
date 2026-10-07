// Supervised bitmap storage: the production storage loop from user/screen, started
// from a RecoveryStart with only the font-extent device right and its IRQ token.
import { RecoveryStart } from "../../src/task/recovery_start.m"
import { storageServe } from "../screen/storage.m"
import { exit } from "../syscalls.m"
import { RECOVERY_START_BYTES, RUNTIME_START_MAGIC } from "../../src/arch/wrm081632/defs.m"

let recoveryBitmapMain(start: *RecoveryStart, bytes: UWord): Void {
    if bytes != RECOVERY_START_BYTES || start.magic != RUNTIME_START_MAGIC ||
        start.generation == 0 || start.reference == 0 || start.irq == 0 exit(1)
    storageServe(start.endpoint, start.irq)
}
export { recoveryBitmapMain }

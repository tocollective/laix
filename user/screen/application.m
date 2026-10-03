import { ServiceStart, serviceStartValid } from "../../src/task/service_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_CLIENT } from "../../src/arch/wrm081632/defs.m"
import { screenWrite } from "client.m"
import { exit } from "../syscalls.m"

let applicationMain(start: *ServiceStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != SERVICE_START_BYTES ||
        !serviceStartValid(start) || start.role != START_ROLE_CLIENT) exit(1)
    if screenWrite(start.endpoint, "LA/IX microkernel v1.0.0\n", 25) != 25 exit(1)
    // Supplementary Unicode is valid even when the selected font lacks it;
    // the user font component selects its validated replacement glyph.
    if screenWrite(start.endpoint, "Unicode: Я 日本 😀\n", 24) != 24 exit(1)
    exit(0)
}

export { applicationMain }

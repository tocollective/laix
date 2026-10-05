// Acceptance application: concurrent requests execute the compiled Input helper.
import { ServiceStart, serviceStartValid } from "../../../src/task/service_start.m"
import { START_BLOCK_VA, SERVICE_START_BYTES, START_ROLE_CLIENT } from "../../../src/arch/wrm081632/defs.m"
import { inputEvents } from "../../../user/services/client.m"
import { sleep, exit } from "../../../user/syscalls.m"
let inputClient(start: *ServiceStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != SERVICE_START_BYTES ||
        !serviceStartValid(start) || start.role != START_ROLE_CLIENT) exit(1)
    let mut events: UWord[4]
    let mut flags: UWord
    for i: UWord in 0..8 {
        let count: Word = inputEvents(start.bitmapEndpoint, &mut events[0], &mut flags)
        if count < 0 || count > 4 exit(2)
        if sleep(1) != 0 exit(3)
    }
    exit(0)
}
export { inputClient }

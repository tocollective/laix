// Acceptance-only application. Real compiled helpers and hardware timer sleeps.
import { TaskStart, taskStartBlockValid } from "../../../src/task/start.m"
import { ServiceStart, serviceStartValid } from "../../../src/task/service_start.m"
import { START_BLOCK_VA, START_VERSION, START_ROLE_CLIENT } from "../../../src/arch/wrm081632/defs.m"
import { consoleWrite } from "../../../user/console.m"
import { screenWrite } from "../../../user/screen/client.m"
import { exit, sleep } from "../../../user/syscalls.m"
let STRESS_ROUNDS: UWord = 128
let stressClient(start: *TaskStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || start.role != START_ROLE_CLIENT) exit(1)
    let screen: Bool = start.version != START_VERSION
    if screen {
        if !serviceStartValid(start as *ServiceStart) exit(2)
    } else if !taskStartBlockValid(start) exit(3)
    let mut text: UByte[2]
    text[0] = ('A' as UWord + start.taskId) as UByte
    text[1] = 10
    for i: UWord in 0..STRESS_ROUNDS {
        let mut count: Word
        if screen count = screenWrite(start.endpoint, &text[0], 2)
        else count = consoleWrite(start.endpoint, &text[0], 2)
        if count != 2 exit(4)
        // Every round must cross an actual timer deadline, even if IPC is fast.
        if sleep(1) != 0 exit(5)
    }
    exit(0)
}
export { stressClient, STRESS_ROUNDS }

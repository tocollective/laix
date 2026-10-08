// Init, the root server (docs/INIT.md). The kernel starts this one task and gives
// it the authority to create every catalog image, to hand out device grants and to
// create endpoints. Init decides which services run, wires their endpoints and
// handle lists, and supervises them. The kernel keeps no task graph of its own.
//
// The build asks for one session through the storage root; the kernel passes its
// number as init's start argument (user/init/sessions.m). An unknown number is an
// error, not the default.
import { RuntimeStart } from "../../src/task/runtime_start.m"
import { START_BLOCK_VA, RUNTIME_START_BYTES, RUNTIME_START_MAGIC, RUNTIME_START_VERSION,
    ERRNO_EINVAL } from "../../src/arch/wrm081632/defs.m"
import { exit } from "../syscalls.m"
import { sessionReset, sessionTeardown, sessionSupervise } from "lib.m"
import { recoverySupervisorRun } from "../recovery/supervisor.m"
import { screenSupervisorRun } from "../recovery/screen_supervisor.m"
import { sessionConsole, sessionServices, sessionLoader, sessionFs, sessionShell, sessionNet,
    sessionScreen } from "session.m"
import { SESSION_SHELL, SESSION_CONSOLE, SESSION_SERVICES, SESSION_LOADER, SESSION_FS, SESSION_NET,
    SESSION_SCREEN, SESSION_RECOVERY, SESSION_SCREENRECOVERY } from "sessions.m"

let initBuild(session: UWord): Word {
    if session == SESSION_SHELL return sessionShell()
    if session == SESSION_CONSOLE return sessionConsole()
    if session == SESSION_SERVICES return sessionServices()
    if session == SESSION_LOADER return sessionLoader()
    if session == SESSION_FS return sessionFs()
    if session == SESSION_NET return sessionNet()
    if session == SESSION_SCREEN return sessionScreen()
    return -ERRNO_EINVAL
}

let initMain(start: *RuntimeStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != RUNTIME_START_BYTES ||
        start.magic != RUNTIME_START_MAGIC || start.version != RUNTIME_START_VERSION) exit(1)
    sessionReset()
    // Two sessions are supervision policy in the first place: init itself is the
    // supervisor (user/recovery) and does not return.
    if start.argument == SESSION_RECOVERY recoverySupervisorRun()
    if start.argument == SESSION_SCREENRECOVERY screenSupervisorRun()
    // If anything fails, every child created so far is terminated or discarded.
    if initBuild(start.argument) < 0 {
        sessionTeardown()
        exit(2)
    }
    // When a member that should keep running ends, the rest is stopped and init
    // reports it through its exit code, so a half-running system is never left.
    let ended: Word = sessionSupervise()
    sessionTeardown()
    exit(ended)
}
export { initMain }

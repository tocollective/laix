// User-only wrappers: import constants, never kernel code or the boot runtime.
import { SYS_DEBUG_PUT_CHAR, SYS_EXIT, SYS_YIELD } from "../src/arch/wrm081632/defs.m"

// One UART byte, 0..255. Returns 0 or -EINVAL; no user pointer is read.
let debugPutChar(code: UWord): Word {
    return syscall(SYS_DEBUG_PUT_CHAR, code)
}

// No successful return. Stay in user mode if a broken kernel returns anyway.
let exit(code: Word): Void {
    syscall(SYS_EXIT, code)
    while true {}
}

// Voluntarily rotate the ready queue. Returns 0 when this task resumes.
let yield(): Word {
    return syscall(SYS_YIELD)
}

export { debugPutChar, exit, yield }

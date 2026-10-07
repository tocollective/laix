// Client of the Exec service (docs/SHELL.md): run a stored program and wait.
import { EXEC_RUN_HEADER, EXEC_RESPONSE_HEADER, DATA_GENERATION, FS_NAME_BYTES,
    ERRNO_EINVAL, ERRNO_EPROTO } from "../../src/arch/wrm081632/defs.m"
import { call } from "../syscalls.m"
import { wordsPut } from "../words.m"

type ExecResult {
    code: Word, // the child's exit code
    faulted: Bool,
    cause: UWord, // trap cause when it faulted
}

let mut execClientRequest: UWord[8]
let mut execClientResponse: UWord[8]

// Runs the file `name` with a numeric argument and waits up to `limit` seconds
// (zero: no limit). Returns zero with the result filled in, or a negative errno:
// -ENOENT no such file, -EINVAL not a loadable image, -ENFILE no room or quota,
// -ETIMEDOUT the program was terminated at the limit.
let execRun(handle: UWord, name: *UByte, argument: UWord, limit: UWord, result: *mut ExecResult): Word {
    if name == null || result == null return -ERRNO_EINVAL
    for w: UWord in 0..8 execClientRequest[w] = 0
    let mut length: UWord = 0
    while name[length] != 0 {
        if length == FS_NAME_BYTES return -ERRNO_EINVAL
        wordsPut(&mut execClientRequest[4], length, name[length] as UWord)
        length += 1
    }
    if length == 0 return -ERRNO_EINVAL
    execClientRequest[0] = EXEC_RUN_HEADER
    execClientRequest[1] = DATA_GENERATION
    execClientRequest[2] = argument
    execClientRequest[3] = limit
    let got: Word = call(handle, &execClientRequest[0] as *UByte, 32, &mut execClientResponse[0] as *mut UByte, 32)
    if got < 0 return got
    if got != 32 || execClientResponse[0] != EXEC_RESPONSE_HEADER || execClientResponse[2] != DATA_GENERATION return -ERRNO_EPROTO
    let status: Word = execClientResponse[1] as Word
    if status > 0 || (status < 0 && execClientResponse[3] != 0) return -ERRNO_EPROTO
    if status != 0 return status
    if execClientResponse[4] > 1 return -ERRNO_EPROTO
    result.code = execClientResponse[3] as Word
    result.faulted = execClientResponse[4] != 0
    result.cause = execClientResponse[5]
    return 0
}
export { ExecResult, execRun }

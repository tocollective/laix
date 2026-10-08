// A command shell as an ordinary application (docs/SHELL.md). It owns the
// keyboard (raw HID events from the kernel's Input broker), writes text through
// the console service, keeps files in the filesystem service and runs programs
// through Exec. It holds no authority beyond those handles.
//
// Start handles (start list): [0] filesystem, [1] Exec, [2] console, all send.
import { RuntimeStart } from "../../src/task/runtime_start.m"
import { START_BLOCK_VA, RUNTIME_START_BYTES, RUNTIME_START_MAGIC, RUNTIME_START_VERSION,
    ERRNO_ENOENT, ERRNO_EEXIST, ERRNO_ENOSPC, ERRNO_EROFS, ERRNO_EINVAL, ERRNO_ENODEV,
    ERRNO_ETIMEDOUT, ERRNO_ENFILE } from "../../src/arch/wrm081632/defs.m"
import { inputRead, yield, exit } from "../syscalls.m"
import { startHandle } from "../starthandles.m"
import { keymapAscii, KEY_SHIFT_LEFT, KEY_SHIFT_RIGHT, KEY_RELEASE } from "../keymap.m"
import { textStart, textFlush, textChar, textString, textLine, textNumber, textSigned } from "../text.m"
import { FsFile, FsInfo, fsOpen, fsReadAt, fsSync, fsRemove, fsListAt, fsInfo, fsReadFile,
    fsPut, fsWriteFile } from "../services/fsclient.m"
import { ExecResult, execRun } from "../services/execclient.m"

let LINE_MAX: UWord = 100
let ARGS_MAX: UWord = 8
let DATA_MAX: UWord = 8192
let RUN_LIMIT_SECONDS: UWord = 60

let mut shellFs: UWord
let mut shellExec: UWord
let mut shellBatch: UWord[34]
let mut shellQueue: UWord[32]
let mut shellQueueHead: UWord
let mut shellQueueCount: UWord
let mut shellShift: Bool
let mut shellLine: UByte[101]
let mut shellLength: UWord
let mut shellArgs: UWord[8] // offset of each word in shellLine
let mut shellArgc: UWord
let mut shellData: UByte[8192]
let mut shellName: UByte[16]
let mut shellSize: UWord[1]
let mut shellFile: FsFile
let mut shellInfo: FsInfo
let mut shellResult: ExecResult

// ---- Keyboard ------------------------------------------------------------

// The next key press that produces a character, as ASCII. Waits (yielding) for events.
let shellKey(): UWord {
    while true {
        if shellQueueCount == 0 {
            let got: Word = inputRead(&shellBatch[0] as *mut UByte, 32)
            if got < 0 exit(6)
            if got == 136 && shellBatch[0] <= 32 {
                for i: UWord in 0..shellBatch[0] shellQueue[i] = shellBatch[2 + i]
                shellQueueHead = 0
                shellQueueCount = shellBatch[0]
            }
            if shellQueueCount == 0 {
                yield()
                continue
            }
        }
        let event: UWord = shellQueue[shellQueueHead]
        shellQueueHead += 1
        shellQueueCount -= 1
        let usage: UWord = event & 0xFFFF
        let release: Bool = event & KEY_RELEASE != 0
        if usage == KEY_SHIFT_LEFT || usage == KEY_SHIFT_RIGHT {
            shellShift = !release
            continue
        }
        if release continue
        let c: UWord = keymapAscii(usage, shellShift)
        if c != 0 return c
    }
}

let shellPrompt(): Void {
    textString("$ ")
    textFlush()
}

// Redraws the line after an erase: the console has no backspace, so the cursor
// goes back to the start of the line, the old text is overwritten with a space
// at its end, and the line is written again.
let shellRedraw(): Void {
    textChar(13)
    textString("$ ")
    for i: UWord in 0..shellLength textChar(shellLine[i] as UWord)
    textChar(32)
    textChar(13)
    textString("$ ")
    for i: UWord in 0..shellLength textChar(shellLine[i] as UWord)
    textFlush()
}

let shellReadLine(): Void {
    shellLength = 0
    while true {
        let c: UWord = shellKey()
        if c == 10 {
            textLine()
            textFlush()
            shellLine[shellLength] = 0
            return
        }
        if c == 8 {
            if shellLength != 0 {
                shellLength -= 1
                shellRedraw()
            }
        } else if c >= 32 && c < 127 && shellLength < LINE_MAX - 1 {
            shellLine[shellLength] = c as UByte
            shellLength += 1
            textChar(c)
            textFlush()
        }
    }
}

// ---- Words ---------------------------------------------------------------

let shellParse(): Void {
    shellArgc = 0
    let mut i: UWord = 0
    while shellLine[i] != 0 {
        if shellLine[i] == 32 {
            shellLine[i] = 0
            i += 1
        } else {
            if shellArgc < ARGS_MAX {
                shellArgs[shellArgc] = i
                shellArgc += 1
            }
            while shellLine[i] != 0 && shellLine[i] != 32 i += 1
        }
    }
}

let shellArg(index: UWord): *UByte {
    return &shellLine[shellArgs[index]]
}

let shellEquals(left: *UByte, right: *UByte): Bool {
    let mut i: UWord = 0
    while left[i] == right[i] {
        if left[i] == 0 return true
        i += 1
    }
    return false
}

// Decimal digits only, at most nine of them.
let shellNumber(text: *UByte, value: *mut UWord): Bool {
    let mut i: UWord = 0
    let mut result: UWord = 0
    while text[i] != 0 {
        if text[i] < 48 || text[i] > 57 || i == 9 return false
        result = result * 10 + (text[i] as UWord - 48)
        i += 1
    }
    if i == 0 return false
    value[0] = result
    return true
}

// ---- Messages ------------------------------------------------------------

let shellError(status: Word): Void {
    textString("error: ")
    if status == -ERRNO_ENOENT textString("no such file")
    else if status == -ERRNO_EEXIST textString("file exists")
    else if status == -ERRNO_ENOSPC textString("no space")
    else if status == -ERRNO_EROFS textString("read-only")
    else if status == -ERRNO_EINVAL textString("invalid")
    else if status == -ERRNO_ENODEV textString("no filesystem")
    else if status == -ERRNO_ETIMEDOUT textString("timed out")
    else if status == -ERRNO_ENFILE textString("no room to run")
    else {
        textString("code ")
        textSigned(status)
    }
    textLine()
}

let shellUsage(text: *UByte): Void {
    textString("usage: ")
    textString(text)
    textLine()
}

// ---- Commands ------------------------------------------------------------

let shellHelp(): Void {
    textString("ls                 list files")
    textLine()
    textString("cat NAME           show a file")
    textLine()
    textString("write NAME TEXT    replace a file with TEXT")
    textLine()
    textString("cp FROM TO         copy a file")
    textLine()
    textString("mv FROM TO         move a file")
    textLine()
    textString("rm NAME            delete a file")
    textLine()
    textString("df                 free space")
    textLine()
    textString("sync               commit changes")
    textLine()
    textString("echo TEXT          print TEXT")
    textLine()
    textString("run NAME [N]       run a program")
    textLine()
    textString("NAME [N]           same as run")
    textLine()
}

let shellList(): Void {
    for index: UWord in 0..31 {
        let status: Word = fsListAt(shellFs, index, &mut shellName[0], &mut shellSize[0])
        if status == -ERRNO_ENOENT return
        if status != 0 {
            shellError(status)
            return
        }
        textNumber(shellSize[0], 8)
        textString("  ")
        for i: UWord in 0..16 {
            if shellName[i] != 0 textChar(shellName[i] as UWord)
        }
        textLine()
    }
}

let shellCat(name: *UByte): Void {
    let opened: Word = fsOpen(shellFs, name, 0, 0, &mut shellFile)
    if opened != 0 {
        shellError(opened)
        return
    }
    let mut offset: UWord = 0
    let mut last: UWord = 10
    while offset < shellFile.size {
        let count: Word = fsReadAt(shellFs, shellFile.id, offset, &mut shellName[0], 16)
        if count <= 0 {
            shellError(count)
            return
        }
        for i: UWord in 0..count as UWord {
            last = shellName[i] as UWord
            textChar(last)
        }
        offset += count as UWord
    }
    if last != 10 textLine()
}

let shellDf(): Void {
    let status: Word = fsInfo(shellFs, &mut shellInfo)
    if status != 0 {
        shellError(status)
        return
    }
    textString("sectors ")
    textNumber(shellInfo.dataSectors, 0)
    textString("  free ")
    textNumber(shellInfo.freeSectors, 0)
    textString("  generation ")
    textNumber(shellInfo.generation, 0)
    if shellInfo.flags & 1 == 0 textString("  read-only")
    if shellInfo.flags & 6 != 0 textString("  uncommitted")
    textLine()
}

// Joins the words from `first` on with single spaces, then a newline.
let shellJoin(first: UWord): UWord {
    let mut length: UWord = 0
    for index: UWord in first..shellArgc {
        if index != first {
            shellData[length] = 32
            length += 1
        }
        let word: *UByte = shellArg(index)
        let mut i: UWord = 0
        while word[i] != 0 && length < DATA_MAX - 2 {
            shellData[length] = word[i]
            length += 1
            i += 1
        }
    }
    shellData[length] = 10
    return length + 1
}

let shellWrite(): Void {
    let length: UWord = shellJoin(2)
    let status: Word = fsWriteFile(shellFs, shellArg(1), &shellData[0], length)
    if status != 0 shellError(status)
}

// Copy, and with remove set, move: the new file and the removal share one commit.
let shellCopy(remove: Bool): Void {
    if shellEquals(shellArg(1), shellArg(2)) {
        textString("error: same file")
        textLine()
        return
    }
    let size: Word = fsReadFile(shellFs, shellArg(1), &mut shellData[0], DATA_MAX)
    if size < 0 {
        if size == -ERRNO_EINVAL textString("error: file too big")
        else shellError(size)
        if size == -ERRNO_EINVAL textLine()
        return
    }
    let mut status: Word = fsPut(shellFs, shellArg(2), &shellData[0], size as UWord)
    if status == 0 && remove status = fsRemove(shellFs, shellArg(1))
    if status == 0 status = fsSync(shellFs)
    if status != 0 shellError(status)
}

let shellRun(name: *UByte, argument: UWord): Void {
    let status: Word = execRun(shellExec, name, argument, RUN_LIMIT_SECONDS, &mut shellResult)
    if status == -ERRNO_ENOENT {
        textString(name)
        textString(": not found")
        textLine()
        return
    }
    if status != 0 {
        shellError(status)
        return
    }
    if shellResult.faulted {
        textString("fault, cause ")
        textNumber(shellResult.cause, 0)
    } else {
        textString("exit ")
        textSigned(shellResult.code)
    }
    textLine()
}

let shellExecute(): Void {
    shellParse()
    if shellArgc == 0 return
    let command: *UByte = shellArg(0)
    if shellEquals(command, "help") shellHelp()
    else if shellEquals(command, "ls") shellList()
    else if shellEquals(command, "df") shellDf()
    else if shellEquals(command, "sync") {
        let status: Word = fsSync(shellFs)
        if status != 0 shellError(status)
    } else if shellEquals(command, "echo") {
        if shellArgc > 1 {
            let length: UWord = shellJoin(1)
            for i: UWord in 0..length textChar(shellData[i] as UWord)
        } else textLine()
    } else if shellEquals(command, "cat") {
        if shellArgc != 2 shellUsage("cat NAME")
        else shellCat(shellArg(1))
    } else if shellEquals(command, "write") {
        if shellArgc < 3 shellUsage("write NAME TEXT")
        else shellWrite()
    } else if shellEquals(command, "rm") {
        if shellArgc != 2 shellUsage("rm NAME")
        else {
            let mut status: Word = fsRemove(shellFs, shellArg(1))
            if status == 0 status = fsSync(shellFs)
            if status != 0 shellError(status)
        }
    } else if shellEquals(command, "cp") {
        if shellArgc != 3 shellUsage("cp FROM TO")
        else shellCopy(false)
    } else if shellEquals(command, "mv") {
        if shellArgc != 3 shellUsage("mv FROM TO")
        else shellCopy(true)
    } else {
        // `run NAME [N]` or a bare program name.
        let mut first: UWord = 0
        if shellEquals(command, "run") {
            if shellArgc < 2 {
                shellUsage("run NAME [N]")
                return
            }
            first = 1
        }
        let mut argument: UWord = 0
        if shellArgc > first + 1 && !shellNumber(shellArg(first + 1), &mut shellSize[0]) {
            shellUsage("NAME [N]")
            return
        }
        if shellArgc > first + 1 argument = shellSize[0]
        shellRun(shellArg(first), argument)
    }
}

let shellLoop(): Void {
    while true {
        shellPrompt()
        shellReadLine()
        shellExecute()
        textFlush()
    }
}

let shellMain(start: *RuntimeStart, bytes: UWord): Void {
    if ((start as UWord) != START_BLOCK_VA || bytes != RUNTIME_START_BYTES ||
        start.magic != RUNTIME_START_MAGIC || start.version != RUNTIME_START_VERSION) exit(1)
    shellFs = startHandle(start, 0)
    shellExec = startHandle(start, 1)
    let console: UWord = startHandle(start, 2)
    if shellFs == 0 || shellExec == 0 || console == 0 exit(2)
    textStart(console)
    textString("LA/IX shell. Type help.")
    textLine()
    shellLoop()
}
export { shellMain, shellLoop }

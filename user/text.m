// Buffered text output over the console service. One console call carries at most
// 28 bytes, so text is collected and sent when the buffer is full or on a flush.
// The console accepts printable ASCII, TAB, LF and CR; any other byte is shown as '.'.
import { consoleWrite } from "console.m"
import { discard } from "syscalls.m"

let TEXT_LIMIT: UWord = 28

let mut textBuffer: UByte[28]
let mut textLength: UWord
let mut textHandle: UWord

let textStart(handle: UWord): Void {
    textHandle = handle
    textLength = 0
}

let textFlush(): Void {
    if textLength != 0 {
        discard(consoleWrite(textHandle, &textBuffer[0] as *UByte, textLength))
        textLength = 0
    }
}

let textChar(c: UWord): Void {
    let mut shown: UWord = c
    if !(c == 9 || c == 10 || c == 13 || (c >= 32 && c < 127)) shown = 46
    textBuffer[textLength] = shown as UByte
    textLength += 1
    if textLength == TEXT_LIMIT textFlush()
}

let textString(text: *UByte): Void {
    let mut i: UWord = 0
    while text[i] != 0 {
        textChar(text[i] as UWord)
        i += 1
    }
}

let textLine(): Void { textChar(10) }

// Decimal, right-aligned to `width` columns (zero: no padding).
let textNumber(value: UWord, width: UWord): Void {
    let mut digits: UByte[10]
    let mut count: UWord = 0
    let mut rest: UWord = value
    while true {
        digits[count] = (48 + rest % 10) as UByte
        count += 1
        rest = rest / 10
        if rest == 0 break
    }
    let mut pad: UWord = 0
    while pad + count < width {
        textChar(32)
        pad += 1
    }
    while count != 0 {
        count -= 1
        textChar(digits[count] as UWord)
    }
}

let textSigned(value: Word): Void {
    if value < 0 {
        textChar(45)
        textNumber((-value) as UWord, 0)
    } else textNumber(value as UWord, 0)
}

export { textStart, textFlush, textChar, textString, textLine, textNumber, textSigned }

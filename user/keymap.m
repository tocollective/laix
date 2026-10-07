// USB HID usage (page 0x07) to ASCII for a US keyboard. Returns zero for keys
// that produce no character. Control keys come out as their ASCII code:
// Enter 10, Backspace 8, Tab 9.
let KEY_SHIFT_LEFT: UWord = 0xE1
let KEY_SHIFT_RIGHT: UWord = 0xE5
let KEY_RELEASE: UWord = 0x80000000

let keymapShiftedDigits: UWord[10] = [33, 64, 35, 36, 37, 94, 38, 42, 40, 41]
// Usages 0x2D..0x38: - = [ ] \ # ; ' ` , . /
let keymapPunctuation: UWord[12] = [45, 61, 91, 93, 92, 35, 59, 39, 96, 44, 46, 47]
let keymapShiftedPunctuation: UWord[12] = [95, 43, 123, 125, 124, 126, 58, 34, 126, 60, 62, 63]

let keymapAscii(usage: UWord, shift: Bool): UWord {
    if usage >= 4 && usage <= 29 {
        if shift return 65 + usage - 4
        return 97 + usage - 4
    }
    if usage >= 30 && usage <= 39 {
        if shift return keymapShiftedDigits[usage - 30]
        if usage == 39 return 48
        return 49 + usage - 30
    }
    if usage == 40 return 10
    if usage == 42 return 8
    if usage == 43 return 9
    if usage == 44 return 32
    if usage >= 45 && usage <= 56 {
        if shift return keymapShiftedPunctuation[usage - 45]
        return keymapPunctuation[usage - 45]
    }
    return 0
}
export { KEY_SHIFT_LEFT, KEY_SHIFT_RIGHT, KEY_RELEASE, keymapAscii }

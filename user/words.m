// Little-endian byte access to word arrays: IPC messages and names are word
// arrays on both sides of every copy, so one access width is used throughout.
let wordsGet(words: *UWord, index: UWord): UWord {
    return (words[index >> 2] >> ((index & 3) << 3)) & 255
}
let wordsPut(words: *mut UWord, index: UWord, value: UWord): Void {
    let shift: UWord = (index & 3) << 3
    words[index >> 2] = (words[index >> 2] & ~(255 << shift)) | (value << shift)
}
export { wordsGet, wordsPut }

// User-only text output. Exactly one bounded, synchronous request/reply call.
// Returns the byte count or -errno; length must not exceed CONSOLE_TEXT_MAX.
// Text permits printable ASCII, TAB, LF and CR. It is never NUL-terminated.
// An unmapped nonempty source faults only this user task, as with other local
// memory accesses. Earlier UART bytes cannot be rolled back after a fault.
extern let consoleWrite(handle: UWord, text: *UByte, length: UWord): Word

export { consoleWrite }

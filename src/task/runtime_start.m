// Pointer-free runtime ABI; reference is authority-neutral, slot diagnostic.
type RuntimeStart {
    magic: UWord,
    version: UWord,
    bytes: UWord,
    reference: UWord,
    slot: UWord,
    data: UWord,
    dataBytes: UWord,
    endpoint: UWord,
    rights: UWord,
    argument: UWord,
}

type TaskEvent {
    reference: UWord,
    slot: UWord,
    state: UWord,
    code: Word,
    flags: UWord,
    cause: UWord,
    epc: UWord,
    badaddr: UWord,
    status: UWord,
    ptbr: UWord,
    fcsr: UWord,
}

export { RuntimeStart, TaskEvent }

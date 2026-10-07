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

// Remaining lifetime of finite, never-reset identities. "Remaining" counts
// admissions or constructions still possible; zero means retired.
type LifetimeReport {
    bytes: UWord,
    limit: UWord, // last valid 23-bit generation (reply, task, handle)
    replySelected: UWord, // admitted calls left in the selected task's reply namespace
    replyTotal: UWord, // calls left over namespaces the caller can still construct into
    replyOpen: UWord, // number of those namespaces
    taskSelected: UWord, // task-reference generations left in the selected slot
    handleSelected: UWord, // fewest handle-slot generations left in the selected table
    retiredTasks: UWord, // task slots with an exhausted reply or reference counter
    retiredHandles: UWord, // exhausted handle slots in all tables
    retiredEndpoints: UWord,
    retiredIrqs: UWord,
}

export { RuntimeStart, TaskEvent, LifetimeReport }

// Recovery startup extends the unchanged ten-word RuntimeStart prefix.
// Instance is the task reference; generation names the resource incarnation.
type RecoveryStart {
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
    dependency: UWord,
    irq: UWord,
    generation: UWord,
    supervisor: UWord,
    blob: UWord, // read-only device-table blob mapped for this task, else zero
    blobBytes: UWord,
}
type ServiceResolution {
    handle: UWord,
    instance: UWord,
    generation: UWord,
    name: UWord,
}
export { RecoveryStart, ServiceResolution }

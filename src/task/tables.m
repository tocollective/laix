// Boot-time sizing of everything that has one entry per task slot. Nothing here
// is a compile-time array: the number of slots follows the installed RAM, so a
// 2 MiB machine gets a few dozen and a 128 MiB machine thousands, up to the
// ceiling the task reference layout allows (TASK_SLOTS). The tables are one
// zero-filled run of frames owned by a reserved kernel identity and never freed.
//
// A task needs at least nine frames (directory, tables, three task pages and a
// guarded kernel stack), which is the RAM each slot is budgeted (memorySlots). The tables cost
// about one kilobyte per slot, so roughly one frame in forty.
import { PAGE_SIZE, PAGE_MASK, WORD_BYTES } from "../arch/wrm081632/defs.m"
import { kernelRamEnd, PAGE_NONE, PAGE_KERNEL, allocPageRun, memorySlots, TASK_SLOTS_MIN,
    MemoryBudget, memoryBudgetBind } from "../mm/memory.m"
import { Task, taskTableBind, taskCapacity } from "task.m"
import { TaskControl, taskControlTableBind } from "control.m"
import { Transfer, transferTableBind } from "../ipc/transfer.m"
import { MAX_ENDPOINTS, endpointQueuesBind } from "../ipc/objects.m"

let TABLES_OWNER: UWord = 0xFFFFFFFB

let tablesAlign(bytes: UWord): UWord { return (bytes + 7) & 0xFFFFFFF8 }

// Bytes of every per-slot table for `capacity` slots, in carving order.
let tablesBytes(capacity: UWord): UWord {
    return tablesAlign(capacity * sizeof(Task)) + tablesAlign(capacity * WORD_BYTES) +
        tablesAlign(capacity * sizeof(Transfer)) + tablesAlign(capacity * sizeof(MemoryBudget)) +
        tablesAlign(capacity * sizeof(TaskControl)) + tablesAlign(2 * MAX_ENDPOINTS * capacity * WORD_BYTES)
}

// Called once, after memoryInit. False leaves every table unbound.
let tablesInit(): Bool {
    if taskCapacity != 0 || kernelRamEnd == 0 return false
    let mut capacity: UWord = memorySlots()
    let mut base: UWord = PAGE_NONE
    while true {
        base = allocPageRun(TABLES_OWNER, PAGE_KERNEL, (tablesBytes(capacity) + PAGE_MASK) / PAGE_SIZE)
        if base != PAGE_NONE || capacity <= TASK_SLOTS_MIN break
        capacity = capacity / 2
        if capacity < TASK_SLOTS_MIN capacity = TASK_SLOTS_MIN
    }
    if base == PAGE_NONE return false
    let ready: UWord = base + tablesAlign(capacity * sizeof(Task))
    let transfers: UWord = ready + tablesAlign(capacity * WORD_BYTES)
    let budgets: UWord = transfers + tablesAlign(capacity * sizeof(Transfer))
    let controls: UWord = budgets + tablesAlign(capacity * sizeof(MemoryBudget))
    let queues: UWord = controls + tablesAlign(capacity * sizeof(TaskControl))
    return memoryBudgetBind(budgets, capacity) && transferTableBind(transfers, capacity) &&
        taskControlTableBind(controls, capacity) && endpointQueuesBind(queues, capacity) &&
        taskTableBind(base, ready, capacity)
}
export { tablesInit, tablesBytes }

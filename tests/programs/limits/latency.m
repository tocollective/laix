import { TrapFrame } from "../../../src/trap/trap_frame.m"
// CPU measurement fixture only. Never linked into ordinary images.
import { kernelInit, kernelBootInfo } from "../../../src/kernel/boot.m"
import { bootstrapInit } from "../../../src/kernel/bootstrap.m"
import { panic } from "../../../src/kernel/panic.m"
import { Task, taskStart, tasks, currentTask, MAX_TASKS, TASK_READY, TASK_BLOCKED, TASK_RUNNING,
    taskTerminateChecked } from "../../../src/task/task.m"
import { SpaceBudget, spaceBudgets, mapPage } from "../../../src/mm/mmu.m"
import { MemoryBudget, allocPage, PAGE_USER, memoryBudgetFind, MEMORY_TASK_PAGES } from "../../../src/mm/memory.m"
import { PAGE_SIZE, MEM_VA_START, PTE_RW, PTE_U } from "../../../src/arch/wrm081632/defs.m"

// Build the largest admitted alias/table/frame workload through trusted APIs.
// Preparation itself is not a public syscall and is outside latency samples.
let latencyFillTasks(includeCurrent: UWord): UWord {
    let mut filled: UWord = 0
    for slot: UWord in 0..MAX_TASKS {
        let task: *mut Task = &mut tasks[slot]
        if ((task == currentTask && includeCurrent == 0) || (task.state != TASK_READY && task.state != TASK_BLOCKED && task.state != TASK_RUNNING)) continue
        let mut budget: *mut SpaceBudget = null
        for i: UWord in 0..32 {
            if spaceBudgets[i].directory == (task.directory as UWord) && spaceBudgets[i].owner == task.id {
                budget = &mut spaceBudgets[i]
            }
        }
        if budget == null panic("latency budget missing", null)
        let data: UWord = task.pages[1]
        let mut table: UWord = 0
        while budget.tables < 8 {
            if !mapPage(task.directory, task.id, MEM_VA_START + table * 0x400000, data, PTE_RW | PTE_U) {
                panic("latency sparse table failed", null)
            }
            table += 1
        }
        let mut index: UWord = 1
        while true {
            let page: UWord = allocPage(task.id, PAGE_USER)
            if page == 0 break
            if !mapPage(task.directory, task.id, MEM_VA_START + index * PAGE_SIZE, page, PTE_RW | PTE_U) {
                panic("latency frame mapping failed", null)
            }
            index += 1
        }
        while budget.mappings < 128 {
            if !mapPage(task.directory, task.id, MEM_VA_START + index * PAGE_SIZE, data, PTE_RW | PTE_U) {
                panic("latency alias mapping failed", null)
            }
            index += 1
        }
        let frames: *mut MemoryBudget = memoryBudgetFind(task.id)
        if frames == null || frames.used != MEMORY_TASK_PAGES panic("latency frame quota not filled", null)
        filled += 1
    }
    return filled
}

let latencyRetirePeers(): UWord {
    let mut count: UWord = 0
    for i: UWord in 0..MAX_TASKS {
        if &mut tasks[i] == currentTask || (tasks[i].state != TASK_READY && tasks[i].state != TASK_BLOCKED) continue
        let selected: *TrapFrame = taskTerminateChecked(&currentTask.context, tasks[i].id, 0)
        if selected != &currentTask.context panic("latency selected retiring peer", null)
        count += 1
    }
    return count
}

let main(): Word {
    kernelInit()
    if !bootstrapInit() panic("latency bootstrap failed", null)
    taskStart(kernelBootInfo.clock)
    return 1
}
export { latencyFillTasks, latencyRetirePeers }

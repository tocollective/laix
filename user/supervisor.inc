    .globl supervisorUserEntry
supervisorUserEntry:
; Minimal user policy: launch approved image 1, collect 24 completions, then
; exercise the same path for a fault. No service name/policy is in the kernel.
    li r6, START_BLOCK_VA
    bne r1, r6, .failed
    li r6, RUNTIME_START_BYTES
    bne r2, r6, .failed
    lw r5, 0(r1)
    li r6, RUNTIME_START_MAGIC
    bne r5, r6, .failed
    lw r5, 4(r1)
    li r6, RUNTIME_START_VERSION
    bne r5, r6, .failed
    lw r11, 20(r1)                   ; private data page for completion
    li r12, 0
.launch:
    li r1, 1
    li r9, SYS_TASK_CREATE
    syscall
    bltz r1, .failed
    mv r10, r1                      ; scoped control reference
    li r2, 0
    li r3, 0
    mv r4, r12
    li r6, 24
    bne r12, r6, .configure
    li r4, 0xDEAD
.configure:
    li r9, SYS_TASK_CONFIGURE
    syscall
    bnez r1, .failed
    mv r1, r10
    li r9, SYS_TASK_PUBLISH
    syscall
    bnez r1, .failed
.collect:
    mv r1, r10
    mv r2, r11
    li r9, SYS_TASK_COLLECT
    syscall
    beqz r1, .completed
    li r6, -ERRNO_EAGAIN
    bne r1, r6, .failed
    li r9, SYS_YIELD
    syscall
    j .collect
.completed:
    lw r5, 0(r11)
    bne r5, r10, .failed
    lw r5, 16(r11)
    li r6, 24
    beq r12, r6, .check_fault
    andi r5, r5, TASK_EVENT_FAULT
    bnez r5, .failed
    lw r5, 12(r11)
    bne r5, r12, .failed
    addi r12, r12, 1
    j .launch
.check_fault:
    andi r5, r5, TASK_EVENT_FAULT
    beqz r5, .failed
    lw r5, 20(r11)
    li r6, 3
    bne r5, r6, .failed
    li r1, 0
    j .exit
.failed:
    li r1, 1
.exit:
    li r9, SYS_EXIT
    syscall
.stopped:
    j .stopped

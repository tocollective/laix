; Soak supervisor (G6): the same user policy as the supervisor profile, run for
; SOAK_ROUNDS lifetimes instead of 24. It is a separate image so the accepted
; supervisor code is untouched. Every eighth child faults, the rest exit.
;
; Per round the supervisor itself checks, with the read-only SYS_LIFETIME
; report, that the child's task slot spent exactly one reference generation
; (taskSelected + round == the 23-bit limit), that no reply identity was spent
; (replySelected == limit) and that no slot has retired. It then collects the
; child and checks the completion event. The host probe checks the kernel's
; resulting state; this program is the only per-round check.
    .include "../arch/wrm081632/defs.inc"
    .text
    .align WORD_BYTES
    .globl soakCodeStart, soakCodeEnd

SOAK_ROUNDS = 4096                  ; a multiple of 8
SOAK_FAULTS = SOAK_ROUNDS / 8
LIFETIME_LIMIT = 0x7FFFFF           ; TASK_GENERATION_MAX
; Offsets in the private data page: the completion event comes first.
EVENT_REFERENCE = 0
EVENT_EXIT = 12
EVENT_FLAGS = 16
EVENT_CAUSE = 20
REPORT = 64                         ; LifetimeReport, 44 bytes
REPORT_REPLY_SELECTED = REPORT + 8
REPORT_TASK_SELECTED = REPORT + 20
REPORT_RETIRED_TASKS = REPORT + 28
FAULT_COUNT = 112
NORMAL_COUNT = 116

soakCodeStart:
soakUserEntry:
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
    li r12, 0                        ; round
.round:
    li r6, SOAK_ROUNDS
    beq r12, r6, .finished
    li r1, 1
    li r9, SYS_TASK_CREATE
    syscall
    bltz r1, .failed
    mv r10, r1                      ; scoped control reference
    ; Finite lifetime: this slot has spent exactly `round` generations.
    mv r1, r10
    addi r2, r11, REPORT
    li r9, SYS_LIFETIME
    syscall
    bnez r1, .failed
    lw r5, REPORT_TASK_SELECTED(r11)
    add r5, r5, r12
    li r6, LIFETIME_LIMIT
    bne r5, r6, .failed
    lw r5, REPORT_REPLY_SELECTED(r11)
    bne r5, r6, .failed
    lw r5, REPORT_RETIRED_TASKS(r11)
    bnez r5, .failed
    li r2, 0
    li r3, 0
    mv r4, r12
    andi r5, r12, 7
    li r6, 7
    bne r5, r6, .configure
    li r4, 0xDEAD                   ; approved fault fixture
.configure:
    mv r1, r10
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
    lw r5, EVENT_REFERENCE(r11)
    bne r5, r10, .failed
    shri r5, r10, 8                 ; reference generation is the round
    bne r5, r12, .failed
    andi r5, r12, 7
    li r6, 7
    beq r5, r6, .faulted
    lw r5, EVENT_FLAGS(r11)
    andi r5, r5, TASK_EVENT_FAULT
    bnez r5, .failed
    lw r5, EVENT_EXIT(r11)
    bne r5, r12, .failed
    lw r5, NORMAL_COUNT(r11)
    addi r5, r5, 1
    sw r5, NORMAL_COUNT(r11)
    j .next
.faulted:
    lw r5, EVENT_FLAGS(r11)
    andi r5, r5, TASK_EVENT_FAULT
    beqz r5, .failed
    lw r5, EVENT_CAUSE(r11)
    li r6, 3                        ; the fixture's unaligned read
    bne r5, r6, .failed
    lw r5, FAULT_COUNT(r11)
    addi r5, r5, 1
    sw r5, FAULT_COUNT(r11)
.next:
    addi r12, r12, 1
    j .round
.finished:
    lw r5, FAULT_COUNT(r11)
    li r6, SOAK_FAULTS
    bne r5, r6, .failed
    lw r5, NORMAL_COUNT(r11)
    li r6, SOAK_ROUNDS - SOAK_FAULTS
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
soakCodeEnd:

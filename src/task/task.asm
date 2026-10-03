; Included by task.m, never uses the M boot runtime as user crt0.
; The loader copies ONLY [userCodeStart, userCodeEnd) to a fresh user frame.
; Position independent: data comes in r1; branches remain inside this blob.
    .include "../arch/wrm081632/defs.inc"
    .text
    .align WORD_BYTES
    .globl userCodeStart, userCodeEnd
    .globl taskKernelResume, taskKernelSp
; Trusted cleanup verifies the actual supervisor SP, not the selected slots.
taskKernelSp:
    mv r1, sp
    ret
; Fresh supervisor entry with a private guarded idle stack and zeroed GPRs.
taskKernelResume:
    ; IRET supplied IE=EXL=UM=0. Reaping ran on this stack in restore.
.idle:
    mtcr status, r0
    call taskIdlePoll
    bnez r1, .dispatch
    fence
    ; WRM wakes on the IRQ line even with IE=0. The level stays pending:
    ; no handler can consume it between the empty check and this WFI.
    wfi
    li r1, STATUS_IE
    mtcr status, r1                  ; service the pending IRQ, then recheck
    j .idle
.dispatch:
    j trapRestoreFrame
userCodeStart:
    mv r10, r3                      ; task ID from the private entry ABI
    addi sp, sp, -STACK_ALIGNMENT
    li r3, 1
    sw r3, 0(sp)
    lw r3, 0(sp)
    addi sp, sp, STACK_ALIGNMENT
    sw r3, 0(r1)                    ; observable ready marker in user data
    addi r1, r10, '0'
    li r9, SYS_DEBUG_PUT_CHAR
    syscall
    li r9, SYS_YIELD
    syscall
    addi r1, r10, '0'
    li r9, SYS_DEBUG_PUT_CHAR
    syscall
    li r1, 10
    li r9, SYS_DEBUG_PUT_CHAR
    syscall
    li r1, 0
    li r9, SYS_EXIT
    syscall
.exit_returned:
    j .exit_returned                 ; unreachable under the LA/IX exit ABI
userCodeEnd:

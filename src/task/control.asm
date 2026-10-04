; Approved immutable image catalog. The syscall takes an image ID, never a
; user-supplied kernel address. Each image fits the checked one-page loader.
    .include "../arch/wrm081632/defs.inc"
    .text
    .align WORD_BYTES
    .globl runtimeApprovedStart, runtimeApprovedEnd
runtimeApprovedStart:
    li r6, START_BLOCK_VA
    bne r1, r6, .bad
    li r6, RUNTIME_START_BYTES
    bne r2, r6, .bad
    lw r5, 0(r1)
    li r6, RUNTIME_START_MAGIC
    bne r5, r6, .bad
    lw r5, 4(r1)
    li r6, RUNTIME_START_VERSION
    bne r5, r6, .bad
    lw r1, 36(r1)                    ; argument is this image's exit code
    li r6, 0xDEAD
    beq r1, r6, .fault
    li r9, SYS_EXIT
    syscall
.halt:
    j .halt
.fault:
    lw r1, 1(r0)                    ; approved fault fixture: unaligned read
    j .halt
.bad:
    li r1, -ERRNO_EINVAL
    li r9, SYS_EXIT
    syscall
    j .halt
runtimeApprovedEnd:
    .align WORD_BYTES
    .globl supervisorCodeStart, supervisorCodeEnd
supervisorCodeStart:
    .include "../../user/supervisor.inc"
supervisorCodeEnd:

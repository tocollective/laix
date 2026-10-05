; User-mode helper; included with syscalls.m and independent of kernel code.
    .include "../src/arch/wrm081632/defs.inc"
    .text
    .align WORD_BYTES
    .globl ipcAcceptResult
ipcAcceptResult:
    li r9, SYS_IPC_ACCEPT
    syscall
    ; r4 is the caller's output pointer, preserved by every syscall outcome.
    ; On -EMSGSIZE r2 is a size, never a reply right.
    li r5, 0
    bltz r1, .failed
    mv r5, r2
.failed:
    sw r1, 0(r4)
    sw r5, WORD_BYTES(r4)
    ret

    .align WORD_BYTES
    .globl ipcTryAcceptResult
ipcTryAcceptResult:
    li r9, SYS_IPC_TRY_ACCEPT
    syscall
    li r5, 0
    bltz r1, .failed
    mv r5, r2
.failed:
    sw r1, 0(r4)
    sw r5, WORD_BYTES(r4)
    ret

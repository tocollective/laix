    .text
    .align 4
    .globl _start
_start:
    call recoveryPolicyMain
    li r1, 99
    li r9, 1
    syscall
.halt:
    j .halt

    .globl recoveryDone
recoveryDone:
    ret

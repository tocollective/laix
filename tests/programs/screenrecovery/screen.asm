    .text
    .align 4
    .globl _start
_start:
    call fixtureScreenMain
    li r1, 99
    li r9, 1
    syscall
.halt:
    j .halt

    .globl recoveryFault
recoveryFault:
    lw r1, 1(r0)
    ret

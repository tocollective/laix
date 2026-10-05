    .text
    .align 4
    .globl recoveryFault
recoveryFault:
    lw r1, 1(r0)
    ret

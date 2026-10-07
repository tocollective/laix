    .text
    .align 4
    .globl _start
_start:
    call screenScenarioMain
    li r1, 99
    li r9, 1
    syscall
.halt:
    j .halt

    .globl recoveryDone, screenRendered
recoveryDone:
    ret
screenRendered:
    ret

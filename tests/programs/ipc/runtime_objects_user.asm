    .text
    .align 4
    .globl _start
_start:
    call objectsUserMain
    li r1, 99
    li r9, 1
    syscall
.stopped:
    j .stopped

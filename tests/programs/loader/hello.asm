    .text
    .align 4
    .globl _start
_start:
    call helloMain
    li r1, 1
    li r9, 1
    syscall
.returned:
    j .returned

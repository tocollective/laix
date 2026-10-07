; User entry receives the immutable start record in r1/r2.
    .text
    .align 4
    .globl _start
_start:
    call shellMain
    li r1, 1
    li r9, 1
    syscall
.returned:
    j .returned

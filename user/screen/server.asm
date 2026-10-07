; User entry: bootstrap passes the RO startup record in r1/r2.
    .text
    .align 4
    .globl screenUserEntry
screenUserEntry:
    call screenMain
    li r1, 1
    li r9, 1
    syscall
.returned:
    j .returned

    .text
    .align 4
    .globl _start
_start:
    call memoryUserMain
    li r1, 99
    li r9, 1
    syscall
.stopped:
    j .stopped
    .globl memoryExecute
memoryExecute:
    addi sp, sp, -8
    sw ra, 0(sp)
    jalr r1
    lw ra, 0(sp)
    addi sp, sp, 8
    ret
    .globl memoryPayloadStart, memoryPayloadEnd
memoryPayloadStart:
    li r1, 42
    ret
memoryPayloadEnd:

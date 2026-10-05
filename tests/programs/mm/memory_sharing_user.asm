    .text
    .align 4
    .globl _start
_start:
    call sharingUserMain
    li r1, 99
    li r9, 1
    syscall
.stopped:
    j .stopped
    .globl sharingOwnerGone, sharingBorrowerGone, sharingBothGone
sharingOwnerGone:
    ret
sharingBorrowerGone:
    ret
sharingBothGone:
    ret

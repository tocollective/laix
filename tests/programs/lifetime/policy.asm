    .text
    .align 4
    .globl _start
_start:
    call lifetimePolicyMain
    li r1, 99
    li r9, 1
    syscall
.halt:
    j .halt

; Breakpoint markers for the CPU probe. They have no effect on the task.
    .globl lifetimeArmed, lifetimeReplaced, lifetimeDone
lifetimeArmed:
    ret
lifetimeReplaced:
    ret
lifetimeDone:
    ret

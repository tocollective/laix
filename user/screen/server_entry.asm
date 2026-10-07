; Separate image entry: the logic modules are also linked into supervised images
; that have their own _start.
    .text
    .align 4
    .globl _start
_start:
    j screenUserEntry

    .rodata
    .align 4
    .globl sharingImageStart, sharingImageEnd
sharingImageStart:
    .incbin "../../../build/sharing-user/sharing.elf"
sharingImageEnd:

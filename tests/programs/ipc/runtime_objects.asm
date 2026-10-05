    .rodata
    .align 4
    .globl objectsImageStart, objectsImageEnd
objectsImageStart:
    .incbin "../../../build/objects-user/objects.elf"
objectsImageEnd:

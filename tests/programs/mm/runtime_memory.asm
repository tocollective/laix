    .rodata
    .align 4
    .globl memoryImageStart, memoryImageEnd
memoryImageStart:
    .incbin "../../../build/memory-user/memory.elf"
memoryImageEnd:

    .rodata
    .align 4
    .globl stressImage, stressImageEnd
stressImage:
    .incbin "../../../build/services/stress-client.elf"
stressImageEnd:

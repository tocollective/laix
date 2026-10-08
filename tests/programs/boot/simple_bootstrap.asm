; Trusted boot embeds with private RX/R/RW user segments.
    .rodata
    .align 4
    .globl inputImage, inputImageEnd, diskImage, diskImageEnd
    .globl filesImage, filesImageEnd, simpleApplicationImage, simpleApplicationImageEnd
inputImage:
    .incbin "../../../build/services/input.elf"
inputImageEnd:
    .align 4
diskImage:
    .incbin "../../../build/services/disk.elf"
diskImageEnd:
    .align 4
filesImage:
    .incbin "../../../build/services/files.elf"
filesImageEnd:
    .align 4
simpleApplicationImage:
    .incbin "../../../build/services/simple-application.elf"
simpleApplicationImageEnd:

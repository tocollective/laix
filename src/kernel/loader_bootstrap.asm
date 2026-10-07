; Trusted boot embeds with private RX/R/RW user segments. The loaded child is
; deliberately absent: it lives on the storage volume, not in the kernel image.
    .rodata
    .align 4
    .globl inputImage, inputImageEnd, diskImage, diskImageEnd, filesImage, filesImageEnd, loaderImage, loaderImageEnd
inputImage:
    .incbin "../../build/services/input.elf"
inputImageEnd:
    .align 4
diskImage:
    .incbin "../../build/services/disk.elf"
diskImageEnd:
    .align 4
filesImage:
    .incbin "../../build/services/files.elf"
filesImageEnd:
    .align 4
loaderImage:
    .incbin "../../build/services/loader.elf"
loaderImageEnd:

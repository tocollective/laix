; Trusted embeds, linked for SERVICE_IMAGE_BASE with separate RX/R/RW segments.
    .rodata
    .align 4
    .globl screenImage, screenImageEnd, storageImage, storageImageEnd
    .globl applicationImage, applicationImageEnd
screenImage:
    .incbin "../../../build/services/screen.elf"
screenImageEnd:
    .align 4
storageImage:
    .incbin "../../../build/services/storage.elf"
storageImageEnd:
    .align 4
applicationImage:
    .incbin "../../../build/services/application.elf"
applicationImageEnd:

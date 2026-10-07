; Trusted boot embeds with private RX/R/RW user segments: Input, Disk, the
; filesystem and its acceptance client. The files live on the storage volume.
    .rodata
    .align 4
    .globl inputImage, inputImageEnd, diskImage, diskImageEnd, fsImage, fsImageEnd, fsClientImage, fsClientImageEnd
inputImage:
    .incbin "../../build/services/input.elf"
inputImageEnd:
    .align 4
diskImage:
    .incbin "../../build/services/disk.elf"
diskImageEnd:
    .align 4
fsImage:
    .incbin "../../build/services/fs.elf"
fsImageEnd:
    .align 4
fsClientImage:
    .incbin "../../build/services/fsclient.elf"
fsClientImageEnd:

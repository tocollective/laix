; Trusted boot embeds with private RX/R/RW user segments: Disk, the filesystem,
; Exec and the shell. The console server is the page-bounded image in bootstrap.asm.
    .rodata
    .align 4
    .globl diskImage, diskImageEnd, fsImage, fsImageEnd, execImage, execImageEnd, shellImage, shellImageEnd
diskImage:
    .incbin "../../build/services/disk.elf"
diskImageEnd:
    .align 4
fsImage:
    .incbin "../../build/services/fs.elf"
fsImageEnd:
    .align 4
execImage:
    .incbin "../../build/services/exec.elf"
execImageEnd:
    .align 4
shellImage:
    .incbin "../../build/services/shell.elf"
shellImageEnd:

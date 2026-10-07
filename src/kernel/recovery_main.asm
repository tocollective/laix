    .rodata
    .align 4
    .globl recoveryEchoImage, recoveryEchoEnd, recoveryDiskImage, recoveryDiskEnd
    .globl recoveryFilesImage, recoveryFilesEnd, recoveryPolicyImage, recoveryPolicyEnd
recoveryEchoImage:
    .incbin "../../build/recovery-user/echo.elf"
recoveryEchoEnd:
    .align 4
recoveryDiskImage:
    .incbin "../../build/recovery-user/disk.elf"
recoveryDiskEnd:
    .align 4
recoveryFilesImage:
    .incbin "../../build/recovery-user/files.elf"
recoveryFilesEnd:
    .align 4
recoveryPolicyImage:
    .incbin "../../build/recovery-user/policy.elf"
recoveryPolicyEnd:
    .align 4
; Catalog rows {start, end} become images 2.. in this order; the kernel holds no
; per-image names. Adding an image is a row here, not a kernel code change.
    .globl recoveryCatalog, recoveryCatalogEnd
recoveryCatalog:
    .word recoveryEchoImage, recoveryEchoEnd
    .word recoveryDiskImage, recoveryDiskEnd
    .word recoveryFilesImage, recoveryFilesEnd
    .word recoveryPolicyImage, recoveryPolicyEnd
recoveryCatalogEnd:

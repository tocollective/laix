    .rodata
    .align 4
    .globl screenRecoveryEchoImage, screenRecoveryEchoEnd, screenRecoveryBitmapImage, screenRecoveryBitmapEnd
    .globl screenRecoveryScreenImage, screenRecoveryScreenEnd, screenRecoveryPolicyImage, screenRecoveryPolicyEnd
screenRecoveryEchoImage:
    .incbin "../../build/screen-recovery-user/echo.elf"
screenRecoveryEchoEnd:
    .align 4
screenRecoveryBitmapImage:
    .incbin "../../build/screen-recovery-user/bitmap.elf"
screenRecoveryBitmapEnd:
    .align 4
screenRecoveryScreenImage:
    .incbin "../../build/screen-recovery-user/screen.elf"
screenRecoveryScreenEnd:
    .align 4
screenRecoveryPolicyImage:
    .incbin "../../build/screen-recovery-user/policy.elf"
screenRecoveryPolicyEnd:
    .align 4
; Catalog rows {start, end} become images 2.. in this order: Echo 2, bitmap
; storage 3, Screen 4, supervisor/client 5. The kernel holds no per-image names.
    .globl screenRecoveryCatalog, screenRecoveryCatalogEnd
screenRecoveryCatalog:
    .word screenRecoveryEchoImage, screenRecoveryEchoEnd
    .word screenRecoveryBitmapImage, screenRecoveryBitmapEnd
    .word screenRecoveryScreenImage, screenRecoveryScreenEnd
    .word screenRecoveryPolicyImage, screenRecoveryPolicyEnd
screenRecoveryCatalogEnd:

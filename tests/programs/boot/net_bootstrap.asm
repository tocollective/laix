; Trusted boot embeds with private RX/R/RW user segments: the Ethernet driver, the
; IP stack and the acceptance client. The console server is the page-bounded image
; in bootstrap.asm.
    .rodata
    .align 4
    .globl netdrvImage, netdrvImageEnd, ipImage, ipImageEnd, netClientImage, netClientImageEnd
netdrvImage:
    .incbin "../../../build/services/netdrv.elf"
netdrvImageEnd:
    .align 4
ipImage:
    .incbin "../../../build/services/ip.elf"
ipImageEnd:
    .align 4
netClientImage:
    .incbin "../../../build/services/netclient.elf"
netClientImageEnd:

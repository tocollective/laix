    .include "../../arch/wrm081632/defs.inc"
    .rodata
    .align PAGE_SIZE
fontData:
    .incbin "../../../fonts/unifont-index.laf"
fontDataEnd:
    .align PAGE_SIZE

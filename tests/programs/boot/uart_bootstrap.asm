; Two independent, page-bounded images linked into the trusted boot text.
; The loader copies each range to its OWN RX frame at USER_CODE. Only local
; branches and data/start VAs are used; no kernel calls or absolute code refs.
    .include "../../../src/arch/wrm081632/defs.inc"
    .text
    .align WORD_BYTES
    .globl bootstrapServerStart, bootstrapServerEnd
    .globl bootstrapClientStart, bootstrapClientEnd
bootstrapServerStart:
    li r14, START_ROLE_SERVER
    li r15, RIGHT_RECEIVE
    li r16, DEVICE_UART_TX
    li r6, START_BLOCK_VA
    bne r1, r6, .invalid
    li r6, START_BLOCK_BYTES
    bne r2, r6, .invalid
    mv r8, r1
    lw r5, START_FIELD_MAGIC(r8)
    li r6, START_MAGIC
    bne r5, r6, .invalid
    lw r5, START_FIELD_VERSION(r8)
    li r6, START_VERSION
    bne r5, r6, .invalid
    lw r5, START_FIELD_BYTES(r8)
    bne r5, r2, .invalid
    lw r5, START_FIELD_ROLE(r8)
    bne r5, r14, .invalid
    lw r5, START_FIELD_TASK_ID(r8)
    beqz r5, .invalid
    lw r10, START_FIELD_ENDPOINT(r8)
    beqz r10, .invalid
    lw r5, START_FIELD_RIGHTS(r8)
    bne r5, r15, .invalid
    lw r5, START_FIELD_DEVICES(r8)
    bne r5, r16, .invalid
    lw r11, START_FIELD_DATA(r8)
    li r6, START_DATA_VA
    bne r11, r6, .invalid
    lw r5, START_FIELD_DATA_BYTES(r8)
    li r6, PAGE_SIZE
    bne r5, r6, .invalid
    lw r5, START_FIELD_IPC_LIMIT(r8)
    li r6, IPC_MESSAGE_MAX
    bne r5, r6, .invalid
    lw r5, START_FIELD_PROTOCOL(r8)
    li r6, START_PROTOCOL_CONSOLE
    bne r5, r6, .invalid
.accept:
    mv r1, r10
    mv r2, r11
    li r3, IPC_MESSAGE_MAX
    li r9, SYS_IPC_ACCEPT
    syscall                         ; sleeps until a client call is available
    bltz r1, .invalid
    mv r12, r2                      ; one-use reply token, not an endpoint
    mv r13, r1                      ; delivered size, always at most 32
    li r18, 0                       ; bytes written, including on errors
    li r6, CONSOLE_HEADER_BYTES
    bltu r13, r6, .bad_request       ; never read a short header
    lbu r5, 0(r11)
    li r6, CONSOLE_VERSION
    bne r5, r6, .bad_request
    lbu r5, 1(r11)
    li r6, CONSOLE_WRITE
    bne r5, r6, .bad_request
    lbu r5, 3(r11)
    bnez r5, .bad_request
    lbu r17, 2(r11)
    li r6, CONSOLE_TEXT_MAX
    bltu r6, r17, .too_large
    addi r6, r17, CONSOLE_HEADER_BYTES
    bne r13, r6, .bad_request
    li r19, 0
.validate:
    beq r19, r17, .validated
    add r7, r11, r19
    lbu r5, CONSOLE_HEADER_BYTES(r7)
    li r6, 9
    beq r5, r6, .valid_byte
    li r6, 10
    beq r5, r6, .valid_byte
    li r6, 13
    beq r5, r6, .valid_byte
    li r6, 32
    bltu r5, r6, .bad_request
    li r6, 127
    bgeu r5, r6, .bad_request
.valid_byte:
    addi r19, r19, 1
    j .validate
.validated:
    beq r18, r17, .success
    add r7, r11, r18
    lbu r1, CONSOLE_HEADER_BYTES(r7)
    li r9, SYS_DEBUG_PUT_CHAR        ; only this image has UART TX authority
    syscall
    bltz r1, .respond
    addi r18, r18, 1
    j .validated
.success:
    li r1, 0
    j .respond
.too_large:
    li r1, -ERRNO_EMSGSIZE
    j .respond
.bad_request:
    li r1, -ERRNO_EINVAL
.respond:
    li r6, CONSOLE_RESPONSE_HEADER
    sw r6, IPC_MESSAGE_MAX(r11)
    sw r1, IPC_MESSAGE_MAX+4(r11)
    sw r18, IPC_MESSAGE_MAX+8(r11)
    mv r1, r12
    addi r2, r11, IPC_MESSAGE_MAX
    li r3, CONSOLE_RESPONSE_BYTES
    li r9, SYS_IPC_REPLY
    syscall                         ; cancellation may make this token stale
    j .accept
.invalid:
    li r1, 1
    li r9, SYS_EXIT
    syscall
.exit_returned:
    j .exit_returned
bootstrapServerEnd:

bootstrapClientStart:
    li r14, START_ROLE_CLIENT
    li r15, RIGHT_SEND
    li r16, 0
    li r6, START_BLOCK_VA
    bne r1, r6, .invalid
    li r6, START_BLOCK_BYTES
    bne r2, r6, .invalid
    mv r8, r1
    lw r5, START_FIELD_MAGIC(r8)
    li r6, START_MAGIC
    bne r5, r6, .invalid
    lw r5, START_FIELD_VERSION(r8)
    li r6, START_VERSION
    bne r5, r6, .invalid
    lw r5, START_FIELD_BYTES(r8)
    bne r5, r2, .invalid
    lw r5, START_FIELD_ROLE(r8)
    bne r5, r14, .invalid
    lw r5, START_FIELD_TASK_ID(r8)
    beqz r5, .invalid
    lw r10, START_FIELD_ENDPOINT(r8)
    beqz r10, .invalid
    lw r5, START_FIELD_RIGHTS(r8)
    bne r5, r15, .invalid
    lw r5, START_FIELD_DEVICES(r8)
    bne r5, r16, .invalid
    lw r11, START_FIELD_DATA(r8)
    li r6, START_DATA_VA
    bne r11, r6, .invalid
    lw r5, START_FIELD_DATA_BYTES(r8)
    li r6, PAGE_SIZE
    bne r5, r6, .invalid
    lw r5, START_FIELD_IPC_LIMIT(r8)
    li r6, IPC_MESSAGE_MAX
    bne r5, r6, .invalid
    lw r5, START_FIELD_PROTOCOL(r8)
    li r6, START_PROTOCOL_CONSOLE
    bne r5, r6, .invalid
    ; The application owns the normal boot banner and uses the public helper.
    call .banner_address
.banner_address:
    addi r2, ra, .banner-.banner_address
    mv r1, r10
    li r3, .banner_end-.banner
    call consoleWrite
    li r6, .banner_end-.banner
    bne r1, r6, .invalid
    li r1, 0
    li r9, SYS_EXIT
    syscall
.exit_returned:
    j .exit_returned
.invalid:
    li r1, 1
    li r9, SYS_EXIT
    syscall
    j .exit_returned
.banner:
    .ascii "LA/IX microkernel v1.0.0\n"
.banner_end:
    .align WORD_BYTES
    .include "../../../user/console_write.inc"
bootstrapClientEnd:

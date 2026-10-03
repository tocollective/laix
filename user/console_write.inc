; Position-independent public M ABI helper shared by the embedded application.
; r1=send handle, r2=text pointer, r3=byte count. One call, at most 28 bytes.
; Only caller-saved registers are used; private aligned buffers live on stack.
    .globl consoleWrite
consoleWrite:
    li r4, CONSOLE_TEXT_MAX
    bltu r4, r3, .too_large
    beqz r3, .prepare
    beqz r2, .bad_pointer
.prepare:
    addi sp, sp, -64
    sw ra, 56(sp)
    sw r1, 44(sp)
    sw r3, 48(sp)
    li r4, CONSOLE_REQUEST_HEADER
    sw r4, 0(sp)
    sb r3, 2(sp)
    mv r6, r2
    mv r7, r3
    addi r8, sp, CONSOLE_HEADER_BYTES
.copy:
    beqz r7, .call
    lbu r4, 0(r6)
    sb r4, 0(r8)
    addi r6, r6, 1
    addi r8, r8, 1
    addi r7, r7, -1
    j .copy
.call:
    lw r1, 44(sp)
    mv r2, sp
    lw r3, 48(sp)
    addi r3, r3, CONSOLE_HEADER_BYTES
    addi r4, sp, IPC_MESSAGE_MAX
    li r5, CONSOLE_RESPONSE_BYTES
    li r9, SYS_IPC_CALL
    syscall
    bltz r1, .return                 ; transport failure, no response to read
    li r6, CONSOLE_RESPONSE_BYTES
    bne r1, r6, .bad_response
    lw r6, IPC_MESSAGE_MAX(sp)
    li r7, CONSOLE_RESPONSE_HEADER
    bne r6, r7, .bad_response
    lw r1, IPC_MESSAGE_MAX+4(sp)
    lw r6, IPC_MESSAGE_MAX+8(sp)
    lw r7, 48(sp)
    bltu r7, r6, .bad_response
    bltz r1, .return                 ; failure may follow a partial device write
    bnez r1, .bad_response
    bne r6, r7, .bad_response
    mv r1, r6
    j .return
.bad_response:
    li r1, -ERRNO_EPROTO
.return:
    lw ra, 56(sp)
    addi sp, sp, 64
    ret
.too_large:
    li r1, -ERRNO_EMSGSIZE
    ret
.bad_pointer:
    li r1, -ERRNO_EFAULT
    ret

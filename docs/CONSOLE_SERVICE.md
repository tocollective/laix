# User UART console service

Normal boot runs a separate user server and application. The application prints
`LA/IX microkernel v1.0.0\n` through `consoleWrite`, then exits. Kernel main only
initializes the kernel, constructs tasks and enters the scheduler. Screen/font
code remains available for dedicated tests; normal boot performs no screen,
font, glyph-disk or RNG demonstration. The optional `LAIX_SESSION=screen` boot
runs separate user screen and bitmap services, described in the
[screen/IRQ/DMA contract](SCREEN_IRQ_DMA.md). Its distinct UTF-8 protocol
does not change the default UART protocol or grants.

## Protocol 2, version 1

Startup `protocol=START_PROTOCOL_CONSOLE=2` identifies this contract. Protocol 1
was the one-byte bootstrap smoke test and is no longer accepted at startup.
The transport remains the checked Service `call`/`accept`/`reply` ABI. Both
messages fit `IPC_MESSAGE_MAX=32`; fields convey no authority or user pointers.

| Request offset | Size | Value |
| --- | --- | --- |
| 0 | 1 | Version 1 |
| 1 | 1 | Type 1: write |
| 2 | 1 | Text byte length, 0..28 |
| 3 | 1 | Reserved, must be zero |
| 4 | length | Text, without a terminator |

The received size must equal `4 + length` exactly. Text permits printable
ASCII (32..126), TAB (9), LF (10) and CR (13). NUL, escape, other controls,
DEL and bytes above 127 are rejected. UTF-8 and terminal command protocols are
deferred. An empty text is a valid request and still completes request/reply.
The server checks the complete header, exact size and every text byte before
printing any byte. Short messages never cause a header read beyond delivery;
untrusted lengths never index beyond the 32-byte receive buffer.

| Response offset | Size | Value |
| --- | --- | --- |
| 0 | 1 | Version 1 |
| 1 | 1 | Type 2: result |
| 2 | 1 | Payload length 8 |
| 3 | 1 | Reserved zero |
| 4 | 4 | Little-endian signed status: 0 or negative errno |
| 8 | 4 | Little-endian number of bytes written |

The response is always 12 bytes. Success reports the exact requested text
length. Invalid version/type/reserved fields, short or inconsistent lengths
and invalid text return `-EINVAL` (22), with zero written. A declared text
length above 28 returns `-EMSGSIZE` (90), also with zero written. A request
above 32 bytes is rejected by IPC before service delivery. Device failures
propagate their negative errno and the number of bytes already written;
UART TX currently never blocks, and the server's fixed grant cannot expire
while it runs. Output cannot be rolled back.

## Client function and blocking

`user/console.m` exports the M ABI function:

```m
extern let consoleWrite(handle: UWord, text: *UByte, length: UWord): Word
```

Its assembly implementation in `user/console_write.inc` is also included
unchanged in the embedded application. It uses a private aligned 64-byte
stack frame for the request, response and saved values, preserves the M
callee-saved registers and stack, and makes one atomic `call`. It returns
the byte count on success, transport/service negative errno on failure, or
`-EPROTO` (71) for an inconsistent response header, size, status or count.
Lengths above 28 fail before reading text or issuing IPC. A null pointer with
nonzero length returns `-EFAULT`; zero length does not dereference text. Other
invalid local pointers fault the application under the ordinary user-memory
policy, without panicking the kernel. The function does not split long text.
Callers needing longer output must explicitly divide it into bounded requests.

The client stays Blocked until reply or cancellation, including after the
server accepts its request. The server uses a full 32-byte `accept` buffer and
sleeps Blocked with wait reason `WAIT_IPC_ACCEPT` when no request exists. It
handles one accepted request, prints it and replies before accepting another.
A stale reply after client death is ignored and the loop continues. Endpoint
loss causes the server to exit; existing owner/last-receiver teardown cancels
queued and accepted client calls with `-EPIPE`.

## Ordering and authority

Requests are processed in FIFO **kernel call-admission order**, not task ID,
invocation timestamp or payload identity. The single server completes all UART
bytes of one valid request before beginning another. Timer preemption may
pause the server, but other applications have no UART operation, so their
text cannot interleave inside that request. Malformed requests occupy their
FIFO position and reply without printing. Separate requests from a long
logical write may interleave with another client's requests. The emergency
kernel debug channel is independent and may interrupt normal service output.

Trusted bootstrap grants the server receive-only Service authority and the
narrow `DEVICE_UART_TX` syscall operation. Applications receive only send
handles. Neither side has user MMIO, disk, DMA, IRQ-control or task-creation
access. Payload bytes never select a device, destination task or reply owner.
The one-use reply token comes only from kernel `accept`.

Kernel `panic` and debug output still write supervisor UART directly. Their
module closure is only `panic.m`, `trap_frame.m`, `debug_uart.m`, and constants;
there is no console service, IPC, disk, allocator or user-memory dependency.
See [acceptance](../tests/CONSOLE_SERVICE_ACCEPTANCE.md).

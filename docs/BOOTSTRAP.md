# Embedded service bootstrap

The normal boot path is `kernelInit` -> `bootstrapInit` -> `taskStart`. Init is a trusted kernel component, executed
with IRQs disabled before any user task runs. It loads two distinct,
position-independent images from `src/kernel/bootstrap.asm`; both images and
the immutable `bootstrapResources` policy table are included in the boot image.
No external loader or user-space init authority is needed for this first pair.

## Images, ownership and resources

| Task | Image | Endpoint rights | Device authority |
| --- | --- | --- | --- |
| 1 | Embedded text-console server | Receive only | `DEVICE_UART_TX` |
| 2 | Embedded banner application | Send only | None |

Each task owns its directory, RX code page, RW/NX zero-initialized data page,
RW/NX user stack, guarded private kernel stack and RO/NX startup page. Code
frames are copied separately and have no writable alias while executable.
The tasks use the same user virtual addresses in different roots; their
physical frames, private tables and stacks are distinct. Kernel mappings are
shared supervisor mappings, including UART MMIO, and never acquire U.

The device grant is a narrow operation, not an MMIO mapping or transferable
endpoint. Syscall 0 (`debugPutChar`) now requires `DEVICE_UART_TX` in the
current kernel-owned TCB; it returns `-EPERM` without writing UART when absent.
For an authorized caller it accepts one byte, 0..255, or returns `-EINVAL`.
The kernel's panic/debug UART functions remain independent of this user grant,
IPC, font loading and user memory. Neither task receives DMA, PIC, timer, disk
or task-management authority.

Init creates a Service endpoint bound to task 1. It temporarily holds a root
handle in the server table to install the receive and send handles, then
closes that root before constructing the startup records. Exactly one handle
remains in each table. The server has neither send nor manage rights. Closing
its last receive handle or terminating it revokes the service and cancels
pending calls with `-EPIPE` through the existing endpoint lifecycle.

## Trusted task and memory API

These are kernel functions accepting trusted values and pointers, not syscalls.
Applications cannot invoke them. The supported init API rejects calls with
IRQs enabled and permanently rejects construction/grants after `taskStart`
sets `schedulerStarted`. Endpoint root issuance is sealed at the same entry.

| Function | Contract |
| --- | --- |
| `taskCreateImage(start, end, entryOffset)` | Validate an aligned, nonempty, at-most-one-page range entirely inside embedded kernel text and an aligned entry offset inside that range; allocate a directory, three private pages and kernel stack; return a stable ID or 0. Success leaves the task Created, outside the ready queue. |
| `taskInstallStart(id, block)` | Accept only a Created task, a valid record, its own ID and an exact-rights live Service handle in its own table. Check the bound server identity. Allocate/copy/map its startup page and set the kernel device grant and entry registers. Return false on validation/allocation/mapping failure. |
| `taskPublish(id)` | Require Created and an installed startup page, then enqueue Ready. No allocation occurs. |
| `taskDiscardCreated(id)` | Revoke/release handles and all inactive construction resources, restore alias permissions and leave the slot Empty. Reject published tasks. Handle/object generations are preserved. |
| `taskInitAvailable()` | Require IRQ exclusion, no scheduler, an empty task table and empty ready queue before the fixed boot policy runs. |

Memory issuance is part of image/start construction: the existing allocator
assigns every frame an owner and purpose, and mapping APIs check that ledger.
Init never accepts user-selected physical pages, owners, directories or device
addresses. No task-create, memory-allocation or arbitrary task-control syscall
is installed; unknown syscall numbers return `-ENOSYS`. A future dynamic init
API will need explicit scoped authority before it can be exposed to user mode.

All fallible work for both tasks finishes before either is published. If image
allocation, endpoint issuance, handle installation or startup mapping fails,
init discards both unqueued records and returns false; kernel main reports the
boot failure. The scheduler cannot run a partially initialized server/client.
Failure does not recycle published task IDs or reset capability generations.
`taskCreate`, `taskPrepare` and `taskBootstrapEndpoints` remain kernel-only
legacy scheduler/raw-IPC test fixtures; normal boot does not call them.

## Startup ABI and discovery

On first user entry r1 is `0x40002000`, r2 is 48 and SP is `0xC0000000`.
Other GPRs, FCSR and reserved frame fields start at zero. The entry is the
image's own RX code at `0x40000000`; writable scratch data is `0x40001000`.
There is no kernel pointer, global endpoint ID or implicit r4 capability.

`TaskStart` in `src/task/start.m` contains twelve little-endian UWords:

| Byte offset | Field | Required value |
| --- | --- | --- |
| 0 | magic | `0x5449584C` |
| 4 | version | 1 |
| 8 | bytes | 48 |
| 12 | role | 1: server; 2: client |
| 16 | taskId | Receiving task's stable ID |
| 20 | endpoint | Positive generation-bearing handle in that task's table |
| 24 | rights | 2: receive for server; 1: send for client |
| 28 | devices | 1: UART TX for server; 0 for client |
| 32 | data | `0x40001000` |
| 36 | dataBytes | 4096 |
| 40 | ipcLimit | 32 |
| 44 | protocol | 2: versioned text-console protocol |

The kernel validates the entire record, exact handle rights, endpoint liveness,
mode and server binding before installing it. Both embedded entry routines
check r1/r2 before dereferencing, then validate the record's fixed fields and
role-specific resources before IPC. The RO/NX page cannot be changed by user
stores, IPC responses or execution. Authority comes from the handle table and
TCB; copying a record or writing a payload cannot create a grant.

Registration/discovery is the fixed init binding: the first client's startup
record already contains its send handle for protocol 2. No registry lookup or
well-known global endpoint number is exposed. Handles can have the same numeric
value in different tables without designating the same authority.

## Console protocol and limits

The [user console contract](CONSOLE_SERVICE.md) defines protocol 2: a versioned,
length-delimited write request with at most 28 text bytes and a 12-byte result.
The embedded application calls the public M ABI `consoleWrite` implementation
and exits after its banner is acknowledged. The server remains Blocked in
`accept`; idle waits for interrupts. The old one-byte protocol is superseded.

See [console acceptance](../tests/CONSOLE_SERVICE_ACCEPTANCE.md), the historical
[bootstrap acceptance](../tests/BOOTSTRAP_ACCEPTANCE.md) and
[service request/reply](IPC_REQUEST_REPLY.md). Screen migration, service restart
and device IRQ/DMA policy remain subsequent work.

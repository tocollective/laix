# Этап 6. Сервисы и драйверы вне ядра

[План ядра](KERNEL.md) · [Предыдущий этап](05_IPC_RIGHTS.md)

Status: the first UART text-console implementation is complete. The
[console protocol](CONSOLE_SERVICE.md) defines bounded messages, errors, client
ABI, FIFO ordering and the independent emergency debug path. Validation is
recorded in [console acceptance](../tests/CONSOLE_SERVICE_ACCEPTANCE.md).
The optional screen boot also implements the eight screen/font, IRQ and DMA
items below: separate user components, exclusive NX grants, notification-based
wait/rearm and a narrow trusted physical-DMA broker. The
[screen/IRQ/DMA contract](SCREEN_IRQ_DMA.md) describes the implementation;
[acceptance](../tests/SCREEN_IRQ_DMA_ACCEPTANCE.md) records 17 dedicated source
checks, 14 screen CPU cases and the 11-case UART regression. Runtime restart remains disabled. The subsequent [simple-service milestone](SIMPLE_SERVICES.md)
adds input, a read-only disk extent and an immutable file endpoint, with
quiescent DMA-owner destruction and independent failure acceptance.

## Implementation

### Service startup and rights

- [x] Trusted bootstrap embeds separate server/application images and resource
  rows: receive + narrow UART TX for the server, send only for applications.
- [x] Each task has private code, data, user/kernel stacks and a directory.
- [x] Task/memory construction is kernel-only and sealed before user entry.
- [x] Validated versioned RO/NX startup records carry the endpoint and policy.
- [x] Discovery uses the application's pre-issued Service handle.

### First console server

- [x] Define request version/type, text length, bounded text, result and errors
  within the stage-5 32-byte IPC limit.
- [x] Run a user-mode accept loop; the server is Blocked while waiting.
- [x] Grant only the narrow UART TX operation to the server; applications
  cannot acquire or access the device directly.
- [x] Implement the public client output function using atomic request/reply.
- [x] Validate the entire message before output, without out-of-buffer reads
  or kernel panic for invalid service input.
- [x] Serialize complete requests in documented FIFO call-admission order.
- [x] Move the ordinary text banner from kernel main into the application;
  normal boot no longer initializes the screen/font demonstration.
- [x] Preserve direct emergency kernel debug UART independent of the service,
  IPC, disk and user memory.

### Screen, IRQ and DMA

The implementation is specified in [SCREEN_IRQ_DMA.md](SCREEN_IRQ_DMA.md).
These eight items are closed against source execution and actual CPU/device
evidence recorded in [acceptance](../tests/SCREEN_IRQ_DMA_ACCEPTANCE.md).

- [x] Move the screen driver, Unicode/font lookup and cache into separate
  user components after the working console protocol; give screen output a
  distinct bounded protocol and keep UART emergency output independent.
- [x] Grant page-bounded VRAM and at most read-only video MMIO only to the
  selected service. Forbid X and unrelated device access through every alias;
  implement device-aware mapping, rollback, revocation and teardown checks.
- [x] Deliver IRQs through authenticated pending notifications and atomic
  wait/wakeup, without calling user functions from the hardware prologue.
- [x] Implement the documented IRQ ownership, masking, level servicing and
  completion/rearm contract. Unserviced and unowned lines stay masked so they
  cannot cause an IRQ storm; servicing must cover all causes on a shared line.
- [x] Enforce the documented DMA trust classes. WRM DMA uses physical
  addresses and bypasses the MMU; unrestricted DMA MMIO grants system trust,
  including the video command page, not merely a task-local device operation.
- [x] Keep dangerous commands behind the narrow trusted broker for the
  untrusted screen/font services. Validate full physical pages/ranges, device
  identity, ownership, alignment, command bits and overflow before submission.
- [x] Implement the physical DMA buffer ledger, pinning, fences, completion
  and quarantine. Forbid free/reuse during an operation, including timeout,
  cancellation and owner death; masking an IRQ does not stop DMA.
- [x] Replace direct glyph disk reads with the bounded bitmap service
  protocol. The console gets no disk DMA registers. The legacy `cacheGlyph`
  pointer is physical only in the kernel identity window; never reuse it as
  a DMA address after moving the buffer into user memory.

The first failure/isolation test uses a server without unrestricted DMA
authority. The [acceptance matrix](../tests/SCREEN_IRQ_DMA_ACCEPTANCE.md)
separates source evidence from actual CPU and device execution.

### Failure and subsequent services

The contracts and limits are specified in [SIMPLE_SERVICES.md](SIMPLE_SERVICES.md)
and the [acceptance record](../tests/SIMPLE_SERVICES_ACCEPTANCE.md).

- [x] Exit/fault terminates the server task, closes/revokes objects according
  to policy and returns `-EPIPE` to queued and accepted clients.
- [x] Preserve handle/object/IRQ generations and reject stale capabilities.
  Runtime restart is unavailable: task slots are terminal and bootstrap is
  sealed. Future restart must issue fresh capabilities explicitly.
- [x] Disable further DMA commands on owner death and wait for device BUSY
  to clear before releasing the pin/buffer or destroying owner resources.
  WRM has no disk abort except machine reset; a stuck device is quarantined.
- [x] Add separate input, read-only disk and immutable file services, each
  with a bounded protocol, exact rights and documented failure behavior.
- [x] Keep networking, general user ELF loading and a full filesystem deferred
  while validating startup, exchange and termination of these simple services.

## Что проверить

- [x] Kernel, user server and separate application execute on CPU (normal boot).
- [x] Application output uses its endpoint; direct UART and MMIO access fail on CPU.
- [x] Server blocks in accept and wakes for application calls on CPU.
- [x] Multiple clients get distinct responses and FIFO complete-message output
  under timer rotations in source execution checks; dedicated CPU ordering
  acceptance remains open.
- [x] Invalid version, type, size and text yield bounded errors; source and CPU checks.
- [x] Inject CPU page faults into Input, Disk and Files; the kernel and
  unrelated services continue, and the application receives an error.
- [x] Queued and accepted clients receive `-EPIPE` on server death; source
  checks cover both states, CPU checks cover a chained in-flight disk call.
- [x] Stale handles are rejected and generations never wrap. Runtime restart
  is excluded by sealed construction/grants; no replacement is implicit.
- [x] Kernel panic emits a complete UART dump with a blocked or dead output
  server; both CPU cases check all 32 GPRs and exit 254.
- [x] Added IRQs retain wakeups at block/rearm boundaries, coalesce each
  unserviced level and reject foreign/stale tokens. Source checks cover the
  boundaries and multi-line owners; CPU checks cover foreign access.
- [x] Added DMA rejects foreign pages, invalid ranges and overflow; pins
  survive early finish, cancellation and repeated reaping until quiescence.
  Source and CPU evidence: [device safety acceptance](../tests/DEVICE_SAFETY_ACCEPTANCE.md).

## Когда этап готов

Первая веха завершена, когда клиент использует отдельный user-консольный
сервер через IPC, ожидание блокируется, права разделены и отказ этого
сервера не останавливает ядро. Перенос остальных драйверов — последующие
вехи с отдельной приёмкой по тем же правилам.
Аппаратные контракты — [UART, PIC, видео и DMA](../../docs/SPECIFICATION.md).

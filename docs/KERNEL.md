# LA/IX microkernel roadmap

Current status: 2026-10-06. The maintained
[readiness matrix](READINESS_MATRIX.md) separates implementation, checked-source,
CPU and bounded sustained-stress evidence. A checked implementation milestone
does not certify every future workload or a different executable artifact.

| Stage | Contract | Current milestone |
| --- | --- | --- |
| 1 | [Boot, TrapFrame and diagnostics](01_BOOT_TRAPS.md) | Supervisor trap/return, trusted emergency stack and complete diagnostics implemented; [historical trap evidence](../tests/ACCEPTANCE.md) and current emergency-UART probes |
| 2 | [Physical memory and MMU](02_MEMORY_MMU.md) | Single-CPU ownership/accounting, W^X, user copies, TLB/ASID rules and scoped runtime memory/sharing implemented; [MMU evidence](../tests/MMU_ACCEPTANCE.md), [A2](../tests/RUNTIME_MEMORY_ACCEPTANCE.md) |
| 3 | [User task and syscall](03_USER_TASK_SYSCALLS.md) | User entry, syscall/exit and local faults accepted; runtime construction/configuration/publication/collection adds scoped reusable lifecycles; [A1](../tests/RUNTIME_TASKS_ACCEPTANCE.md) |
| 4 | [Scheduler, timer and IRQ](04_SCHEDULER_IRQ.md) | Round-robin scheduling, idle WFI and generation-bearing device notifications implemented; [historical 20,000-switch campaign](../tests/SCHEDULER_ACCEPTANCE.md), current A9 timer/IRQ workloads |
| 5 | [IPC and object rights](05_IPC_RIGHTS.md) | Raw and Service transport, scoped runtime factories/transfer, finite reply generations, cancellation/deadlines and explicit reconnection implemented; [current acceptance](../tests/ACCEPTANCE_CI.md) |
| 6 | [User services and drivers](06_USER_SERVICES.md) | UART/Screen/Input/Disk/Files and failure containment implemented. Runtime Disk/Files recovery is explicit; fixed UART/Screen automatic restart remains unsupported. [A5](../tests/SERVICE_RECOVERY_ACCEPTANCE.md), [A7](../tests/DEVICE_BOUNDARY_ACCEPTANCE.md), [A9](../tests/ACCEPTANCE_CI.md) |

## Implemented milestones

- [x] Independent kernel start, trap return and complete panic dump.
- [x] Physical allocation/ownership, MMU APIs and section/alias protection.
- [x] User-mode entry, syscall/exit, local faults and checked user copying.
- [x] Timer preemption, round-robin rotation, idle and task teardown.
- [x] Blocking Raw rendezvous and Service request/reply with scoped rights.
- [x] Separate user console/application images and contained service failure.

Stage 6's first milestone is complete: an application obtains output through
an isolated user service, waits block, rights are separate and service death
does not stop the kernel. Dedicated later campaigns cover device containment,
multiple applications, explicit supervised replacement and bounded stress.
Completed containment, workload validation and unsupported restart policies
must be reported separately.

## Current work and extension rules

The [audit overview](LAIX_MICROKERNEL_AUDIT.md) and
[readiness matrix](READINESS_MATRIX.md) track the runtime architecture. UART
bootstrap remains fixed policy; Screen rendering/font/cache live in user
components with bounded device mechanisms. The runtime graph supports explicit
Disk/Files reconstruction with fresh task/endpoint/IRQ generations and resolver
reconnection. Neither path gives users arbitrary physical DMA/MMIO authority.

Every new lifecycle API needs source regressions and a matching identified CPU
run for races, exhaustion, rollback and repeated reuse. A source-only result
must remain CPU-pending. Artifact-based reruns use an existing approved emulator
and ROM; rebuilding WRM is not acceptance verification.

Future priorities, SMP, cached ASID leases, demand paging, persistent storage,
network services and physical device reset require their own contracts and
acceptance. Bounded stress and measured latency do not establish universal WCET
or infinite capability lifetime.

## Historical evidence and machine contracts

The [original trap report](../tests/ACCEPTANCE.md),
[MMU report](../tests/MMU_ACCEPTANCE.md),
[user entry report](../tests/USER_ACCEPTANCE.md),
[user buffers](../tests/USER_BUFFERS_ACCEPTANCE.md) and
[scheduler report](../tests/SCHEDULER_ACCEPTANCE.md) retain their original
artifact hashes and per-run counts. Their results do not silently certify
later images; [A9 provenance](../tests/ACCEPTANCE_CI_PROVENANCE.json) binds the
current source/compiler/harness snapshots and executable inputs separately.

- [ABI and process startup](../../docs/ABI.md).
- [ISA, exceptions, IRET and MMU](../../docs/INSTRUCTIONS.md).
- [Devices and boot protocol](../../docs/SPECIFICATION.md).
- [M hardware operations and runtime](../../mc/docs/spec/07-hardware.md).

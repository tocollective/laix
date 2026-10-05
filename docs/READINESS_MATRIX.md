# LA/IX readiness and evidence

Maintained by [A9](AUDIT_09_ACCEPTANCE_AND_CI.md). This matrix describes the
supported architecture, rather than treating an old roadmap checkbox as a
current test result. See [the current campaign](../tests/ACCEPTANCE_CI.md) for
identified input bundles and rerun commands. Historical reports retain their
original dates, source counts and hashes.

The preserved A9 campaign passed 399 source tests and all 14 CPU profiles
(20 probe suites). Its [provenance](../tests/ACCEPTANCE_CI_PROVENANCE.json) and
[workload table](../tests/ACCEPTANCE_CI.md#current-campaign-and-scope) identify
which artifacts and bounded workloads support each acceptance claim.
A concurrent `mc/runtime/rt.m` change landed after the run and remains **validation
pending**; the later checkout is rejected by preflight. The exact accepted source
snapshot is preserved for replay, as documented in that report. The rows below
refer to the accepted snapshot, rather than silently accepting later edits.

The four evidence levels are independent:

- **Implementation complete**: code and its scoped contract exist.
- **Source accepted**: checked M/ASM logic and device fixtures pass. This does
  not establish compiled execution, device timing or instruction interleaving.
- **CPU accepted**: a named probe passes on its identified image/map/ELFs,
  emulator and ROM. Acceptance belongs to those bytes and source manifests.
- **Sustained stress accepted**: a specified repeated workload passes its
  progress, ordering and resource invariants. Counts and virtual CPU time are
  part of the claim; they do not establish an indefinite lifetime or WCET.

| Mechanism or claim | Implementation | Source evidence | CPU evidence and profile | Sustained stress and limits |
| --- | --- | --- | --- | --- |
| Boot, trusted trap stack, complete panic diagnostics | Complete | [test_boot](../tests/test_boot.py), [test_kernel](../tests/test_kernel.py), [test_trap_entry](../tests/test_trap_entry.py), stack/null/unexpected-trap harness tests | [Historical trap report](../tests/ACCEPTANCE.md); current UART bootstrap and Screen emergency-UART probes | Fatal paths are bounded cases; no nested traps or kernel preemption |
| Page ownership, W^X, user copying, MMU/TLB isolation | Complete for one CPU | [test_memory](../tests/test_memory.py), [test_page_allocator](../tests/test_page_allocator.py), [test_address_space](../tests/test_address_space.py), [test_wx_aliases](../tests/test_wx_aliases.py), [test_user_memory](../tests/test_user_memory.py) | [MMU report](../tests/MMU_ACCEPTANCE.md), [user buffers](../tests/USER_BUFFERS_ACCEPTANCE.md); current Memory/Sharing and service RO/NX probes | Historical MMU artifacts remain historical; no ASID caching or SMP claim |
| Task construction/configuration/publication/termination/collection | Complete, scoped manager authority | [test_runtime_tasks](../tests/test_runtime_tasks.py), [test_runtime_task_acceptance](../tests/test_runtime_task_acceptance.py), [test_task](../tests/test_task.py), [test_recovery_policy](../tests/test_recovery_policy.py) | Supervisor profile: [probe_runtime_tasks_cpu.py](../tests/probe_runtime_tasks_cpu.py); [A1 record](../tests/RUNTIME_TASKS_ACCEPTANCE.md) | 25 child lifetimes, stale references, fault/reap and final empty resource records |
| Runtime memory and explicit sharing | Complete, eager private regions and named borrowers | [test_runtime_memory](../tests/test_runtime_memory.py), [test_memory_sharing](../tests/test_memory_sharing.py), [test_wx_aliases](../tests/test_wx_aliases.py) | Memory and Sharing profiles; [runtime memory](../tests/RUNTIME_MEMORY_ACCEPTANCE.md), [sharing](../tests/MEMORY_SHARING_ACCEPTANCE.md) | Reuse, revocation, both death orders and rollback; no demand paging or general user loader |
| Runtime endpoints, factory quotas and capability transfer | Complete within published pools | [test_runtime_objects](../tests/test_runtime_objects.py), [test_capability_transfer](../tests/test_capability_transfer.py), [test_ipc_handles](../tests/test_ipc_handles.py) | Objects profile: [probe_runtime_objects_cpu.py](../tests/probe_runtime_objects_cpu.py); [A3](../tests/RUNTIME_OBJECTS_ACCEPTANCE.md), [A6](../tests/CAPABILITY_TRANSFER_ACCEPTANCE.md) | Source tests cover retirement/quota/rollback boundaries; no claim that every transfer race has a separate current CPU case |
| Standalone Raw send/receive | Complete | [test_ipc_transport](../tests/test_ipc_transport.py) | UART: [probe_raw_transport_cpu.py](../tests/probe_raw_transport_cpu.py), seven cases, both arrival orders, capacity/retry, rights, scoped cancellation, actual owner fault and reclamation | 128 maximum-message rendezvous with real timer scheduling and reference/FIFO checks |
| Service call/accept/reply and one-use reply rights | Complete | [test_ipc_request_reply](../tests/test_ipc_request_reply.py), [test_ipc_service_helper](../tests/test_ipc_service_helper.py) | UART request/reply; compiled M `accept` executes in Screen, Services, HID and Recovery. [Historical helper limitation](../tests/IPC_REQUEST_REPLY_ACCEPTANCE.md) applies only to that older image | 128 maximal request/reply exchanges; FIFO, cancellation, stale replies and overlap |
| Timed calls, cancellation, sleep and watchdog policy | Complete | [test_ipc_liveness](../tests/test_ipc_liveness.py) | UART: [probe_ipc_liveness_cpu.py](../tests/probe_ipc_liveness_cpu.py); [A4 record](../tests/IPC_LIVENESS_ACCEPTANCE.md) | 32 natural deadlines, two boundary serializations, FIFO cleanup, death and watchdog progress |
| UART text service and concurrent output | Complete | [test_console_service](../tests/test_console_service.py), [test_bootstrap](../tests/test_bootstrap.py) | UART bootstrap; UART stress uses four separately instantiated compiled M applications with `consoleWrite` | 128 writes and one-second hardware sleeps per client; complete-message FIFO order, exact UART bytes and reaped clients |
| Screen rendering/font/cache in user components | Complete | [test_screen_services](../tests/test_screen_services.py), [test_videocard](../tests/test_videocard.py), [test_console](../tests/test_console.py), [test_unifont](../tests/test_unifont.py) | Screen natural pixels/14 authority cases; Screen stress with four compiled applications | 128 writes per client, concurrent admission/reply order, timer progress and no retained DMA pin; fixed Screen bootstrap has no runtime restart |
| IRQ ownership, coalescing, mask/ack/rearm | Complete for supported level lines | [test_timer_irq](../tests/test_timer_irq.py), [test_device_waits](../tests/test_device_waits.py), [test_device_safety](../tests/test_device_safety.py), [test_device_boundary](../tests/test_device_boundary.py) | Screen forced DONE+VBLANK at rearm and wait publication, plus natural real device IRQs | Forced fixtures explicitly select device level and the armed grant state under EXL; no physical stuck-device/reset campaign |
| DMA cancellation, active owner death, timeout and late completion | Complete, trusted read-only sector broker | [test_device_boundary](../tests/test_device_boundary.py), [test_simple_services](../tests/test_simple_services.py), [test_device_safety](../tests/test_device_safety.py) | Services: BUSY sampled at the real fault, DEAD store and cancellation; delayed physical transfer crosses the five-second IRQ wait | Pins held to physical quiescence; before/after and freed-page canaries survive two further timer IRQs; permanently BUSY containment remains source evidence |
| Medium removal/replacement invalidates authority | Complete for this boot | [test_device_boundary](../tests/test_device_boundary.py), [test_device_safety](../tests/test_device_safety.py), [test_simple_services](../tests/test_simple_services.py) | Media profile: actual snapshot `disk_eject`/`disk_insert` on a compact approved floppy during DMA; client failure, peer survival and pin release | The new cases cover in-flight replacement/removal; between bitmap halves and cache-hit validation remain separate Screen campaign extensions |
| Actual HID arrival and overflow under concurrent clients | Complete | [test_simple_services](../tests/test_simple_services.py), [test_device_boundary](../tests/test_device_boundary.py) | HID profile: host `--input` calls `keyboard_key`; 64 down/up events, real 32-event FIFO, MMIO broker, compiled Input service and five compiled clients | 40 requests, ordered 32-event delivery, 32 drops, overflow reported once, eight seconds of hardware timer progress; SDL physical-keyboard UI routing is outside this script campaign |
| Explicit supervised Disk/Files recovery and unrelated peer progress | Complete for the approved runtime graph | [test_service_recovery](../tests/test_service_recovery.py), [test_recovery_policy](../tests/test_recovery_policy.py), [test_ipc_liveness](../tests/test_ipc_liveness.py) | Recovery fixture and production profiles; [A5 record](../tests/SERVICE_RECOVERY_ACCEPTANCE.md) | Five generations, eight actual faults, explicit reconnects, stale-right rejection and final zero resources. Production remains an ongoing service graph |
| Pool/generation limits and IRQ-disabled latency | Complete for the published limits | [test_limits_latency](../tests/test_limits_latency.py), runtime resource/handle retirement tests | Latency and Services maximum-copy probes; [A8 record](../tests/LIMITS_LATENCY_ACCEPTANCE.md) | Four seven-client rounds, 400 timer samples, maximal populate/map/reap and 512-byte DMA; measured budgets apply to these workloads |
| Fixed UART/Screen automatic restart, physical device reset, writes/flush, persistent filesystem, networking, priorities/SMP | Not implemented/supported by these milestones | Negative authority and contract tests only | No CPU acceptance claimed | Explicit runtime Disk/Files recovery does not implement these extensions |

## Keeping evidence current

A lifecycle/API change must update this matrix and its contract in the same
change. Add checked-source cases for admission/exhaustion, failed publication
and rollback, races with cancellation/death, stale authority and repeated
resource reuse. Add matching CPU cases to the applicable maintained profile;
label the row **source accepted; CPU pending** until that identified run passes.
Do not reuse a previous image's green checkbox for changed code.

Add sustained-stress evidence when the change affects progress or reuse. Record
its iterations, timer/device preconditions and post-teardown conservation.
Conditional future mechanisms, such as cached ASID leases or staged teardown,
need their own race and reuse campaign before being marked implemented.

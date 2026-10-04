# Runtime task lifecycle acceptance

Date: 2026-10-05. Scope: approved embedded-image task creation and supervision.

The twelve implementation items in [A1](../docs/AUDIT_01_TASK_LIFECYCLE.md) are
implemented. The [runtime contract](../docs/RUNTIME_TASKS.md) specifies identity,
authority, ABI, resource ledger, rollback, cancellation, history and quarantine.

## Checked-source coverage

`tests/test_runtime_tasks.py` executes the checked M task/control/IPC/MMU ASTs
with explicit memory and device fixtures; it does not generate instructions.
Its fourteen tests cover:

- Forty sequential child lifetimes with stable free-page counts after every
  reap, distinct generations, stale-reference rejection and retained history.
- Created isolation, RO/NX startup layout, invalid context publication and
  initial endpoint revocation before publication.
- Creation-mask/image checks and foreign configure/publish/inspect/terminate/
  collect denial even when the foreign caller also has creation authority.
- Final exit/fault snapshots, full-range copy validation and no event loss on
  `-EFAULT`; repeated collection is denied.
- Ready FIFO removal and Running self termination with delayed selected-stack
  reaping; raw send/receive, accept, AwaitAccept and AwaitReply cancellation.
- Persistent reply and endpoint-handle generations across slot reuse; old
  physical allocator owners cannot claim a replacement task's pages.
- Failure at every allocation and mapping stage, including cleanup of inherited
  endpoint rights when startup allocation or mapping fails.
- Generation exhaustion without wrap; bounded completion capacity with a slow
  collector; cleanup after supervisor death without killing published children.
- IRQ wait cancellation and a BUSY DMA owner staying Dead with its root, stack
  and bounce buffer intact. Early event collection cannot permit slot reuse;
  only physical completion and broker quiescence permit reclamation.

The initial implementation passed **299 tests** in the full LA/IX source regression
suite, including all fourteen runtime lifecycle tests. Focused lifecycle and
bootstrap rechecks also passed. Shell syntax and `git diff --check` passed.
Existing user-component dependency checks explicitly allow the new
pointer-free `runtime_start.m` ABI without permitting kernel driver imports.
Both existing user-service profiles compile and link successfully with the
extended syscall wrappers. The existing warning about the deliberately
non-returning exit wrapper discarding a syscall result remains unchanged.

## Adversarial acceptance

`test_runtime_task_acceptance.py` adds six checked-source tests for the five
remaining A1 acceptance items. All task-control operations go through
`userSyscall`; grants are established by the normal bootstrap policy before
its seal. The tests do not modify the kernel's authority tables after sealing.

| Requirement | Test and observed result |
| --- | --- |
| Authorized termination in each state | `test_authorized_manager_termination_matrix_completes_each_survivor_once` covers Ready, Running self termination, RawSend, RawReceive, empty Accept and IRQ wait. Two surviving peers each receive `-EPIPE` and one wake; duplicate termination and repeated reaping add no wake. Stack/root remain allocated until selected-stack reaping. Running self termination retains its final history record after its own control capability disappears. |
| AwaitAccept and AwaitReply | `test_await_accept_and_reply_victim_removal_preserves_survivor_fifo` removes the victim through authorized termination, rejects its accepted reply token, preserves peer order, delivers each peer's response once and rejects duplicate replies. The controller collects the victim's final code. |
| Stale references after slot reuse | `test_replacement_rejects_old_control_reply_irq_and_endpoint_tokens` reuses slot 2 as reference 258 while retaining the old completion capability. Old configure/publish/terminate operations, reply tokens, IRQ wait/complete tokens and endpoint call/destroy tokens leave the replacement's resources and data unchanged. The new reply wakes it once. Collecting the old event reports the old reference and cannot control the replacement. |
| Foreign and forged control | `test_foreign_and_forged_controls_preserve_tasks_queues_and_user_memory` checks five operations against seven foreign or malformed references, including an incorrect generation and the sign bit. All return `-EPERM`. Every live task's TCB outside the caller's syscall frame, the target's complete frame, user data, free pages, ready queue and complete task-control table remain unchanged. The caller's ordinary syscall arguments, result and advanced EPC are expected frame changes. |
| Construction failures | `test_every_allocation_and_mapping_failure_rolls_back` in the original lifecycle suite fails every allocator and mapping operation. This reaches boundaries after mailbox/slot reservation, directory and frame allocation, guarded stack allocation, context preparation, inherited rights and startup allocation. The new `test_failures_after_successful_acquisitions_rollback_unpublished_task` additionally returns failure after page acquisition, each of the four successful mappings and complete startup installation. Every observed acquisition leaves the partial task Created, unqueued and absent from the ready queue. Rollback restores free pages, endpoint reference count and control count; a subsequent successful construction advances the failed lifetime's generation. Invalid context or revoked initial rights also prevent publication in the original suite. |
| DMA BUSY quarantine | `test_dma_busy_pins_every_owned_page_and_identity_under_slot_pressure` kills an IRQ-blocked DMA owner, collects its quarantined event, fills all four remaining slots and confirms another create fails. Repeated reaping, late IRQ notification and timeout scanning retain the dead identity, directory, all four user mappings, startup page, stack bounds and bounce buffer. The free-page set shows that no owned directory/table/guard/run/data/buffer page becomes available. Only the fixture's physical completion and broker reaping permit slot 2 to become reference 258. Its old IRQ token remains invalid. |

These cases use the checked M interpreter and a controllable device fixture,
so BUSY can remain held across explicit lifecycle operations. They establish
source-level acceptance, including allocator ownership and queue behavior;
they do not add CPU evidence for the adversarial paths. Runtime IRQ grants
remain sealed: the stale IRQ test rejects an old grant against the new task,
without granting that replacement new hardware authority.

Final adversarial verification: **305 tests passed** in the full LA/IX source
regression suite (304.706 seconds), and all six acceptance tests passed again
after the final snapshot assertions. `git diff --check` passed. This acceptance
extension changes tests and documentation only; WRM was not built, and the
earlier CPU image/probe evidence below was not rerun or extended.

## CPU coverage

`probe_runtime_tasks_cpu.py` uses the existing emulator executable and ROM with
an isolated temporary disk and local monitor. It never builds WRM, modifies
input artifacts or injects user instructions. The supervisor image was built
from the final LA/IX sources with `LAIX_CONSOLE=supervisor sh laix/build.sh`.

The CPU probe passed the unchanged user supervisor's 24 normal exits (codes
0–23), one user alignment fault (CAUSE=3, BADADDR=1), and final supervisor exit
(code 0). It verified distinct task generations, all 25 child completion
records, fault diagnostics, RECLAIMED flags, Empty runtime slots, released
roots and stacks, an empty ready queue, no remaining task-control capabilities,
and final execution on the selected idle stack without a kernel panic.

Generated logs are in `build/acceptance/runtime-tasks/`: `results.json`,
`supervisor.uart.txt`, and `supervisor.monitor.txt`.

| Artifact | SHA-256 |
| --- | --- |
| build/supervisor.img | `5a855955f4e05d2ab22265779bdc96e5a3819e50ba4ee097b4f99fefcf2b7187` |
| build/supervisor.map | `6d6380021cf04f341a0e663c2fa8a10337ccc822902e3612b2500186e0070feb` |
| ../bin/wrm081632 (existing binary) | `5e7d9f7f90ccbc7929b894c118fecbecb04fe58165b16f1843f7412026e2448c` |
| ../bin/firmware.rom (existing ROM) | `cab72f0aa602b35f746555c05289be61cd8271bba11ee4d773c7a625230c9965` |

## Evidence limits

CPU coverage here confirms launch, normal exit, fault, completion collection,
resource reclamation and repeated reuse on the real instruction/trap/MMU path.
Authorized termination, adversarial references, failure injection and prolonged
DMA BUSY quarantine are checked-source/device-fixture coverage in this change;
they are not claimed as new CPU acceptance cases. The five A1 adversarial
acceptance boxes link to the checked-source evidence and its limits above.

Runtime loading currently accepts only catalog image 1; arbitrary image
buffers, dynamic linking, runtime device/IRQ grants, transferable task-control
capabilities and blocking wait/join are outside this scope. Fixed boot fixtures
retain Dead TCB snapshots for compatibility; runtime child slots are reusable.

Reproduce source checks with:

```sh
python3 -m unittest discover -s laix/tests -p 'test_*.py'
```

Reproduce CPU acceptance with the existing emulator and ROM:

```sh
python3 laix/tests/probe_runtime_tasks_cpu.py laix/build/supervisor.img laix/build/supervisor.map
```

# A4 IPC liveness acceptance

Date: 2026-10-05. The [contract](../docs/IPC_LIVENESS.md) defines the new ABI,
clock arithmetic, completion precedence, supervisor scope and retry rules.
The audit [A4](../docs/AUDIT_04_IPC_LIVENESS.md) links to this evidence.

## Implementation evidence

| Requirement | Implementation / contract | Verification |
| --- | --- | --- |
| Monotonic representation, overflow and maximum | `drivers/timer.m`: existing COUNT high/low/high read, carried addition and modular comparison; `IPC_LIVENESS.md`: 1..60 seconds, distance <2^63 | source `test_clock_low_carry_full_wrap_maximum_and_invalid_timeout_no_admission`; CPU `timeouts`, `stress` use natural COUNT deadlines |
| One budget through both call states | `ipcCallTimed`, `ipcCallMode`, `ipcAcceptCall`; trusted `Task.waitTimed`/`waitDeadline` | source pre/post accept test; CPU `timeouts` |
| Raw/accept bounded alternative | `ipcSendMode`/`ipcReceiveMode`/`ipcAcceptMode` with explicit nonblocking syscalls, legacy calls unchanged | source nonblocking validation/delivery test; CPU `watchdog` |
| Bounded expiry and cleanup | `timerInterrupt` -> `ipcTimerTick` -> `ipcFinishWait`; eight records, bounded FIFO shift/snapshot clear | source FIFO/cycle/expiry tests; CPU `fifo`, `cycle`, `stress` |
| Reply right invalidation before Ready | `ipcFinishWait` -> `ipcDetach` -> pin release -> metadata clear -> result -> `taskWake` | source late/new generation tests; CPU `timeouts`, `boundary`, `cancel` |
| Scoped supervisor cancellation | `TASK_RIGHT_CANCEL=32`, `taskControlLookup`, `ipcSupervisorCancel`; termination remains separate | source authority/queued/accepted/untimed/sleep cancellation test and Raw/accept cancellation test; CPU `cancel`, `watchdog` |
| Serialized precedence, FIFO preservation | same terminal routine for reply, timeout, cancellation, revocation and termination; dying caller is not woken | source all event-order tests; CPU `boundary`, `death`, `fifo` |
| Watchdog waiting | `ipcSleep`, `WAIT_SLEEP`, generic wake rejection; try IPC plus inspect/collect polling | source sleep/cycle tests; CPU `watchdog`, `idle_sleep` |
| Retry IDs, duplicates and side effects | contract requires content-bound logical request IDs, incarnation/session identity, duplicate result retention/eviction and explicit unknown outcomes | documentation review; no automatic retry or deduplication is claimed for kernel or existing service protocols |
| Self-call vs general cycles | legacy self-call EDEADLK retained; timed A-to-B-to-A waits recover without cycle detection | existing request/reply self-call test; source cycle test and CPU `cycle` |

## Acceptance evidence

`test_ipc_liveness.py` executes the checked M AST, actual syscall dispatcher,
TCB/endpoint/control tables, scheduler and MMU copies. COUNT advancement is a
fixture in those tests. It establishes deterministic equality and rollover
boundaries, which are distinct from hardware timer evidence.

| Acceptance item | Source test | CPU evidence |
| --- | --- | --- |
| Timeout before/after accept, no references left | `test_pre_and_post_accept_timeout_one_budget_late_reply_and_next_generation` | `timeouts`, alternating pre/post accept in `stress` |
| Reply/expiry boundary, one result/wake, no stale write | `test_reply_expiry_boundary_both_serialized_orders` | `boundary`: set deadline to COUNT inside atomic reply for reply-first; natural IRQ expiry for expiry-first |
| Expiry with service death, client termination and destruction | `test_expiry_revocation_server_death_and_client_termination_both_orders`: both states and both orders for all three events | `death`: client termination in both orders, destruction after expiry, service termination before expiry; source evidence supplies the other permutations |
| Late/repeated replies cannot address a later call | source pre/post timeout test and existing generation tests | `timeouts` exercises old token against a later accepted call; `stress` repeatedly rejects expired rights |
| Cyclic calls and live infinite-loop server | `test_cycle_and_live_loop_restore_progress` | `cycle`: both services blocked; `watchdog`: live noncooperating server, sleep, scoped cancellation and termination; natural timer waits in `stress` |
| Oldest/middle expiry preserves FIFO | `test_oldest_and_middle_expiry_and_cancel_preserve_fifo` | `fifo`: align two trusted deadlines, expire oldest/middle through real timer, accept survivors in order |
| Sustained real CPU timer entry | source tests are supporting evidence only | `stress`: 32 natural one-second deadlines; nine CPU scenarios include real SYSCALL, timer IRQ, IRET and Ready membership checks |
| Cancellation/expiry equality, scoped authority | `test_cancel_expiry_boundary_both_orders`, scoped cancellation test | `cancel`, `watchdog` |
| Sleep, death and idle progress | `test_sleep_no_generic_wake_and_death_no_resurrection` | `idle_sleep`: all six users blocked, idle WFI and real timer wake |
| User ABI including r6 and accept result | wrapper/assembly test plus existing ABI tests | compiled user wrapper module; CPU raw syscall ABI preserves r3..r31/FCSR and consumes EPC once |

## Accepted results

The full source suite passed **341 tests**. A final focused run passed all
**12 liveness tests**, including two boundary/Raw cancellation cases added
after the full suite started. The nine CPU cases passed with
`complete=true` and `acceptance_complete=true`: **43 timer completions**, including
**32 sustained natural deadlines**. Every CPU case checks preserved contexts
and unique Ready membership; source wake counters establish exactly one wake
on each winning live completion and no wake for a terminating blocked client.
The manifest records hashes for both source logs and the CPU result file.

## Reproduction and provenance

Only LA/IX and its user wrapper module were compiled during this work. WRM and
the firmware ROM were used as existing binaries and were not rebuilt.
The CPU probe does not generate or patch instructions. It edits trusted fixture
startup/context/data records in disposable machines. Most deadlines come from
`callTimed` itself. The boundary and FIFO/cycle tests additionally edit trusted
deadlines to select precise serialized orders. They still use real timer entry;
the sustained test never changes its deadlines or COUNT.

From the repository root:

```sh
LAIX_CONSOLE=uart sh laix/build.sh
python3 -B -m unittest discover -s laix/tests -p 'test_*.py'
python3 -B laix/tests/probe_ipc_liveness_cpu.py \
  laix/build/acceptance/ipc-liveness/inputs/laix.img \
  laix/build/acceptance/ipc-liveness/inputs/laix.map \
  --emulator laix/build/acceptance/ipc-liveness/inputs/wrm081632 \
  --rom laix/build/acceptance/ipc-liveness/inputs/firmware.rom \
  --iterations 32 --log-dir laix/build/acceptance/ipc-liveness/verified
```

The tracked [provenance manifest](IPC_LIVENESS_PROVENANCE.json) records source
and compiler hashes, input image/map/emulator/ROM hashes and accepted CPU
outcomes. Matching executable inputs and per-case monitor/UART/emulator logs
are preserved under the ignored `build/acceptance/ipc-liveness/` directory.
Probe input hashes are checked before and after acceptance; mismatches fail the
run. Rebuilding an image may change artifact hashes and requires new acceptance.
Historical acceptance images from other milestones have the previous TCB
layout; preflight deliberately rejects them against the new source layout.

## Limits

Timers use whole-second budgets up to sixty seconds and the virtual clock.
Legacy Raw waits/accept/call remain unbounded; try variants supply the bounded
alternative. There is no asynchronous cancellation object, general cycle
detector, event multiplexer, hard kernel WCET claim, automatic retry, or durable
protocol deduplication. The CPU fixture exercises the kernel ABI through the
existing assembly user blob; new M wrapper and try-accept helper behavior have
source/assembly checks and compilation evidence, not separate M application
execution on CPU. Service recovery policy and restart supervision remain A5.

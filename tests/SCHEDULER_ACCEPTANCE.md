# Scheduler, timer and idle acceptance

Date: 2026-10-04. All CPU checks use the ready image/map pair preserved in
`build/acceptance/scheduler_verified/inputs/`, together with copies of
`../bin/wrm081632` and firmware ROM. No compiler, assembler, linker or build
script was run. Preserved artifacts are hashed before and after each probe.

## Method

`probe_scheduler_cpu.py` boots temporary machines with a local-only monitor,
headless graphics and networking disabled. It preflights the WRMB header,
kernel layout, scheduler symbols, TCB size and existing user blob. Legacy
single-task images are rejected. Source checking supplies field offsets;
runtime comparisons verify the selected records, actual PTBR and entry words.

The CPU executes the existing image's instructions. Monitor changes are limited
to saved return contexts and fixture data in the temporary machine. Yield
checks re-arm the parked task at the existing syscall; timer stress uses the
blob's unchanged final self-branch, so neither task voluntarily releases CPU.
Distinct GPR patterns, FCSR values `0x21`/`0x61` and `tp` pointers are seeded
before entry and compared against independent expected contexts after IRET.

Block/wake are kernel-only APIs. The probe borrows a selected return frame to
invoke these existing functions in supervisor mode with IE clear, using the
selected kernel stack. It copies the original user frame before `taskBlock`
saves it. This is a controlled event fixture, not an implemented IPC or device
wakeup service. Production handlers and instruction bytes are unchanged.

## Coverage

| Requirement | Evidence |
| --- | --- |
| Predictable cooperative order | 32 actual yield syscalls alternate tasks 1 and 2; syscall EPC advances exactly once and r1 returns zero |
| Busy loops cannot monopolize CPU | 20,000 actual timer switches at 1 kHz between two unchanged self-branches; no user yield executes |
| Context stability | Every stress switch checks all 32 GPRs, FCSR, saved hardware EPC, PTBR, selected TCB, kernel stack and Ready queue |
| Same VA, distinct task data | 32 actual user-mode loads at VA `0x40001000` alternate values `0x11111111` and `0x22222222` from separate roots |
| tp/TLS | Stress preserves distinct r28 pointers; the load fixture sets both restored tp and sp to the same task-local VA and executes the existing `lw r3, 0(sp)` |
| No stale translations | CPU loads/stores populate the TLB in both roots, then read the correct task data after selection; source checks also require FENCE/TLBI.ALL/PTBR ordering |
| Queue invariants | CPU probes check Ready membership, queued flags, Running identity, valid IDs and uniqueness; source tests cover eight-task wrap, duplicate wake and illegal transitions |
| Wakeup before/during/after idle decision | `test_idle.py` checks every queue-to-WFI instruction boundary and wake during sleep; CPU lifecycle cases exercise real block/wake and timer wake from idle |
| Idle uses WFI and returns on an event | CPU stops immediately before and after the existing WFI with IE=0; afterward TIMER EXPIRED and the level IRQ are pending, and wakeup resumes a user task |
| Correct selected kernel stack | Hardware-entry and dispatcher-return snapshots match the outgoing stack; selected TCB, PTBR and all three low stack words match the incoming task |
| Exact IRQ EPC | The saved outgoing EPC equals the hardware EPC and the next selected IRET target equals that task's saved EPC |
| EXPIRED acknowledgment and no IRQ storm | Every stress switch checks pending EXPIRED, cleared EXPIRED/line after IRET and COUNT at or beyond the next period deadline; 128 functional switches retain the default 100 Hz quantum |
| Exit/fault isolation and cleanup | Both lifecycle cases kill one task, resume its peer and receive another timer quantum; after both stop, the allocator bitmap equals its baseline plus the three persistent idle pages |
| No live retired stack/wait references | Dead tasks have zero directory, kernel-stack bounds and waitReason; Ready excludes them, and live TCB/PTBR/entry words select the survivor or idle |
| Boot/trap/user regressions with the timer | Natural boot passes the existing self-tests and two-task demo; 18 returning user syscalls cover byte/error endpoints and SP=0/3/unmapped with timer active; the existing supervisor register self-test is repeated with TIMER CONTROL=3 and PIC ENABLE=4 |

Boot initialization intentionally requires IE=0 and PIC ENABLE=0. The timer is
started by `taskStart`, so its boot self-tests run before activation. The later
supervisor BREAK/SYSCALL register self-test runs with PIC/timer enabled and CPU
IE clear, preserving the non-nested kernel contract.

The TLS fixture validates context preservation and task-local addressing.
It does not add a TLS loader, language runtime, or compiler-generated TLS ABI.
Dead TCB context snapshots remain available for diagnostics; they are excluded
from execution and resource ownership. Idle's stack remains allocated by design.

All 179 source/tool tests pass with `python3 -B -m unittest discover -s
laix/tests -p 'test_*.py'`. These include the previous boot, trap, task, syscall,
MMU and idle checks, plus eight probe checks for the new CPU probe. The frozen
stage-3 `probe_user_cpu.py` is not applied to the new scheduler ABI.

The long stress case reprograms RELOAD/CONTROL through existing CPU `memcpy`
MMIO accesses, with a 1 kHz quantum (32,000 ticks for this 32 MHz timer). The
functional cases retain `taskStart`'s 100 Hz quantum (320,000 ticks). A preliminary
20,000-switch run at 100 Hz passed its CPU checks, but image/map files were
replaced during that run and the final artifact comparison correctly rejected
its completion. `scheduler_stress/results.json` retains that incomplete report.
The final runs use preserved copies of the replacement artifacts.

## Reproduction and artifacts

From the parent WRM repository:

```sh
python3 -B laix/tests/probe_scheduler_cpu.py \
  laix/build/acceptance/scheduler_verified/inputs/laix.img \
  laix/build/acceptance/scheduler_verified/inputs/laix.map \
  --emulator laix/build/acceptance/scheduler_verified/inputs/wrm081632 \
  --rom laix/build/acceptance/scheduler_verified/inputs/firmware.rom \
  --switches 20000 --stress-hz 1000

python3 -B -m unittest discover -s laix/tests -p 'test_*.py'
```

The probe needs permission to open its monitor socket on `127.0.0.1`. It never
listens on an external interface. Each machine is killed when its case finishes;
idle is expected to keep running rather than power off.

Recorded runs, outside Git:

- `build/acceptance/scheduler_verified/`: seven CPU cases; 128 timer switches at 100 Hz.
- `build/acceptance/scheduler_verified_stress/`: 20,000 timer switches at 1 kHz.

Each directory contains `results.json`, monitor snapshots, UART and emulator
output. UART logging escapes valid non-UTF-8 debug bytes such as `0xFF`.

| Artifact | SHA-256 |
| --- | --- |
| `inputs/laix.img` | `be8224e9799ad72970dc3ed8be435b3332cb01c6057934499d1ee8221aab51be` |
| `inputs/laix.map` | `b890530e6d284fcf419b8317d54fa55425613e1d388015574aea992532730104` |
| `inputs/wrm081632` | `5e7d9f7f90ccbc7929b894c118fecbecb04fe58165b16f1843f7412026e2448c` |
| `inputs/firmware.rom` | `4e42ef9742fa0b70efca1c8a113482770dda9ce68b113bcf001942a0ede0d0b5` |

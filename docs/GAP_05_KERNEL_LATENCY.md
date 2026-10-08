# G5. The kernel is non-preemptible and non-nesting

[Gap checklist](GAPS_CHECKLIST.md) · [Previous](GAP_04_FINITE_LIFETIMES.md) · [Next](GAP_06_EVIDENCE_AND_CI.md)

Date: 2026-10-07; implemented 2026-10-08 (staged teardown, 20 ms budget; packaged
evidence pending). Priority: **P2**.

## Criterion

Kernel work runs with IRQs excluded, so interrupt latency equals the longest
kernel section. This is a measured regression budget, not a worst-case proof.

## State before G5

From [LIMITS_AND_LATENCY.md](LIMITS_AND_LATENCY.md) and
[the A8 record](../tests/LIMITS_LATENCY_ACCEPTANCE.md):

- Trap entry uses static scratch and is not nesting-safe; there is no kernel
  preemption and no recoverable kernel fault (a supervisor fault panics).
- The largest measured section was 94.880117 ms at 128 MHz: EXIT plus cleanup
  of eight maximally charged tasks in one section. The budget was 500 ms.
- No staged (resumable) teardown existed.

## What changed

- **Staged teardown.** `taskReap` commits at most one dead root per call
  (`TASK_REAP_STAGE_TASKS`) and says whether another waits. Dead tasks keep
  their root, frames, stack and charges until their own stage. Stages resume at
  the next trap return or, in the idle loop, after an IRQ window that
  `taskIdlePoll` requests with `TASK_IDLE_STAGE`. Details and the visible delay
  of later reclamation: [Staged teardown](LIMITS_AND_LATENCY.md#staged-teardown-g5).
- **Tighter budget with a guard.** The section budget is 2,560,000 cycles
  (20 ms), down from 64,000,000 (500 ms). The longest measured section is now a
  full `SYS_TASK_LOAD` at 1,859,714 cycles (14.5 ms); the same eight-task exit
  is 1,555,589 cycles plus seven idle stages of at most 1,550,591. The probe
  also enforces per-kind ceilings, and `test_staged_reap` checks that the
  probe constants, the docs and the other probes agree.
- **Nesting-safe trap protocol: not built.** No selected workload needs it (see
  below).

## Evidence

| Claim | Evidence |
|---|---|
| One root per section, the rest pinned and intact | [test_staged_reap](../tests/test_staged_reap.py): per-stage page ownership for seven dead tasks, conservation of free pages, slot order |
| Interruption between stages | Same file: creation between stages never takes a pinned slot; a timer IRQ in the idle window loses and repeats no stage; the idle assembly polls again before it sleeps |
| Cancellation between stages | Termination arriving mid-teardown joins the set; second terminate and early collect see `ESRCH` and an unreclaimed completion; a BUSY-DMA task neither spends a stage nor keeps idle awake |
| Real timing | `probe_limits_latency_cpu.py` (profile `latency`, 32 MiB and 128 MiB): seven `reap_stage` samples, EXIT stages exactly one root, longest timer gap 3,177,135 cycles (24.8 ms) |
| Reuse and sustained stress | `soak` profile (4096 task lifetimes, 512 faults) and the other profiles listed in the checklist, rerun on the staged kernel |

All of it is local to the changed tree and outside any identified bundle; the
provenance records and CI profile lists have not been regenerated.

## Decision: nesting-safe trap protocol

Not required. The [target workload](TARGET_WORKLOAD.md) is interactive with no
real-time class; its longest IRQ-excluded section is now 14.5 ms against a
10 ms timer period. Making trap entry nesting-safe means replacing the static
`TRAP_SAVED_R1` slot, giving each nesting level a frame and stack discipline,
and defining what a nested fault is. That is a new entry path to verify for
latency the workload does not ask for. Reopen it only if a workload needs an
interrupt latency below the populate/load section (about 14 ms), and then also
stage or shrink those two sections first, since they set the floor.

## Remaining limits

- Populate (64 KiB) and `SYS_TASK_LOAD` are single sections of 13.7 and 14.5 ms.
  They are bounded by the 64 KiB region limit, not staged.
- A root is one stage. Splitting it per table would not lower the budget while
  populate and load stay as they are.
- Full TLB invalidation on every switch; no ASID caching (needs a lease and
  stale-translation probes, [MMU](02_MEMORY_MMU.md)); not needed at these costs.
- Reclamation of the second and later dead task of a burst is delayed by one
  stage each (a trap return or idle poll), so a supervisor must poll for
  `RECLAIMED`, as `retireClient` does, and not assume frames are free on return
  from a termination.
- No claim of hard real-time or WCET is made.

## Done when

The longest IRQ-disabled section is reduced or its budget is justified for
the target workload (done: 20 ms, justified by 14.5 ms measured), a staged
teardown passes interruption/cancellation tests without exposing partial
mappings (done at source level, with CPU runs), and the latency table covers the
supported RAM sizes (done at 32 and 128 MiB for memory/reaping). The G5 CPU runs
are packaged in the G7 campaign ([checklist](GAPS_CHECKLIST.md)); the A8
provenance record stays historical.

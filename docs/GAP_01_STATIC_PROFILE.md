# G1. The microkernel is complete only for a narrow static profile

[Gap checklist](GAPS_CHECKLIST.md) · [Next](GAP_02_KERNEL_POLICY.md)

Date: 2026-10-07. Priority: **P2**.

## Criterion

"Complete" holds for a statically bounded, single-CPU, one-thread-per-task
system. A general-purpose workload may need more than these bounds allow.

## Current state

The published bounds are in [LIMITS_AND_LATENCY.md](LIMITS_AND_LATENCY.md):

- 8 task slots (6 ordinary plus 2 recovery-reserved), 16 endpoints, 16 handles
  per task, 8 IPC waits per endpoint, 0–32-byte messages.
- 96 physical frames, 128 user mappings and 8 private page tables per task.
- Runtime image creation uses an immutable catalog: at most 3 `PT_LOAD`
  entries and 256 KiB. Since 2026-10-08 a supervisor can also load an ELF it
  read from Files ([FILES_LOADER.md](FILES_LOADER.md)), for run-time children.
- FIFO round-robin with no priorities, donation, CPU budgets or threads.
  The extensions were evaluated and deferred in
  [OPTIONAL_EXTENSIONS.md](OPTIONAL_EXTENSIONS.md).
- Latency was measured at 2 MiB and 32 MiB, and since 2026-10-08 at 128 MiB for
  the memory/reaping workload; 128 MiB is a deliberate machine limit, not a defect.

The selected target workload and its comparison with these bounds are in
[TARGET_WORKLOAD.md](TARGET_WORKLOAD.md). It adopts the profile the accepted
campaigns already exercise, so no bound is raised yet.

## Consequence

A workload with more than six concurrent applications, large images or bulk
transfers cannot run, and the failure is a documented `ENFILE`/`ENOMEM`, not a
bug. Which bounds are enough is a workload question that is not yet answered.

## Fix directions

- Record the target workload (application count, image sizes, transfer sizes)
  and compare it with the table above.
- Make pool sizes a build-time profile only if the workload exceeds them; each
  change must re-measure the section budget. Done 2026-10-08: the
  workload does not exceed them, so no pool changed.
- ~~Add latency acceptance at the maximum supported RAM size.~~ Done 2026-10-08
  for the kernel latency workload (128 MiB, see
  [LIMITS_AND_LATENCY.md](LIMITS_AND_LATENCY.md#timing-envelope-and-retained-correctness-baseline)).
- ~~Replace the image catalog with a loader that reads from the Files service,
  keeping the bounded-copy and rollback rules.~~ Done 2026-10-08 for run-time
  children ([FILES_LOADER.md](FILES_LOADER.md)); the boot catalog remains, and
  Files serves one file.
- Revisit [OPTIONAL_EXTENSIONS.md](OPTIONAL_EXTENSIONS.md) entry conditions
  when the workload is chosen.

## Done when

The supported profile (counts, sizes, RAM range) is written down, every bound
is either confirmed sufficient or raised with new measurements, and the
readiness matrix states the supported RAM range with matching evidence.

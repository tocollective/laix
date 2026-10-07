# LA/IX remaining gaps and fix checklist

Date: 2026-10-07; G2, G3, G4 and G5 updated 2026-10-08. The [A1–A10 audit](LAIX_MICROKERNEL_AUDIT.md) closed the
lifecycle baseline. These gaps are the conditions under which "complete
microkernel" still needs qualification. Each is a proposal; a box is checked only
where the line says what was done and how. Order is a suggestion, not a plan.

| ID | Gap | Priority |
| --- | --- | --- |
| [G1](GAP_01_STATIC_PROFILE.md) | Complete only for a narrow static profile | P2 |
| [G2](GAP_02_KERNEL_POLICY.md) | Device and loader policy still in the kernel (mostly done; see checklist) | P2 |
| [G3](GAP_03_FIXED_SERVICE_RESTART.md) | Fixed UART/Screen boots cannot restart (supervised Screen done; see checklist) | P2 |
| [G4](GAP_04_FINITE_LIFETIMES.md) | Finite identity and reply lifetimes (done, including a local CPU run; bundle pending) | P2 |
| [G5](GAP_05_KERNEL_LATENCY.md) | Non-preemptible, non-nesting kernel (staged teardown and 20 ms budget done; nesting-safe entry decided not needed; bundle pending) | P2 |
| [G6](GAP_06_EVIDENCE_AND_CI.md) | Evidence tied to artifacts; remote CI not run | P2 |
| [G7](GAP_07_APPLICATION_LAYER.md) | No application/storage layer | P3 |

## Suggested order

Do G6 first so later work lands on a verified tree, then G1 to choose the
workload that decides G2, G4 and G5, then G3, then G7.

## Checklist

### G6 Evidence
- [x] Re-run the source suite and artifact replay on the current tree; resolve the `rt.m` validation-pending state (2026-10-07, 399 tests, 14/14 CPU profiles).
- [ ] Provision the four CI variables and record one remote CPU run.
- [x] Add a longer soak profile for reaping and finite-lifetime workloads (2026-10-08; `soak` profile: 4096 task lifetimes with 512 faults in one boot, per-round `SYS_LIFETIME` and event checks, [probe_soak_cpu.py](../tests/probe_soak_cpu.py)). Local run and local bundle replay on the changed tree; not an identified remote run, and not yet in the checked-in provenance record.

### G1 Static profile
- [x] Write the target workload (applications, image sizes, transfer sizes, RAM range): [TARGET_WORKLOAD.md](TARGET_WORKLOAD.md), 2026-10-07. Duty-cycle and latency class are assumptions pending confirmation.
- [x] Confirm each pool bound or raise it with new section measurements (2026-10-08; [TARGET_WORKLOAD](TARGET_WORKLOAD.md): every bound has a verdict and none was raised. Four children per creator is enough for the profile; the open 64 KiB Files read was measured, 7.10 s for 4,096 reads, so bulk reads are not supported; duty and latency assumptions adopted as the profile. Local run outside any bundle).
- [x] Add latency acceptance at the maximum supported RAM size (2026-10-08; `latency` profile now also runs `probe_limits_latency_cpu.py --ram 32M,32M,32M,32M`: the guest reports 134,217,728 bytes and 32,342 free frames, every section's maximum is cycle-identical to 32 MiB, largest 12,350,615 cycles = 96.5 ms against the 500 ms budget). Local run and local bundle replay on the changed tree; not an identified remote run.
- [x] Replace the image catalog with a Files-backed loader (2026-10-08; [FILES_LOADER](FILES_LOADER.md): `SYS_TASK_LOAD` plus a user loader that reads the ELF over Disk and Files; 10 source tests, `loader` CPU profile: 11 children from the volume, 3 tampered volumes refused, longest call 14.5 ms. For run-time children only: the boot catalog still starts Input, Disk, Files and the loader, and Files serves one file, so one program per volume. Local run outside any bundle).

### G2 Kernel policy
- [x] Table-driven device resource descriptors replace Screen-specific constants (2026-10-08, static boot table; [test_device_table](../tests/test_device_table.py)).
- [x] Producer-neutral storage root format (16-byte `WSR1` record, [storage_root.py](../tools/storage_root.py)).
- [x] Image catalog supplied as data (build-issued `ImageRow` rows loaded by `taskCatalogLoad`). Not yet supplied at runtime by a user-mode manager; that needs the G1 loader.
- [x] Multi-sector and write/flush contract defined before any writable filesystem ([contract](DEVICE_CONTRACT.md#multi-sector-and-writeflush-contract); implemented 2026-10-08 for G7, source accepted).
- [ ] Package the G2 source and CPU runs into an identified acceptance bundle (G6 rules).

### G3 Fixed service restart
- [x] Runtime regrant of Screen resources after owner death with quiescence preflight (2026-10-08; `grantTaskDevices(SCREEN)`, [test_screen_recovery](../tests/test_screen_recovery.py)). Supervised `screenrecovery` profile only; the fixed boots stay sealed.
- [x] Screen in the recovery policy (`recoverChain`: bounded attempts/backoff, explicit reconnect); bitmap storage is replaced with it (shared generation).
- [x] Decide UART: documented as fixed ([G3](GAP_03_FIXED_SERVICE_RESTART.md)); the emergency UART is independent either way.
- [x] Specify device reset against the WRM hardware spec: none exists, only the machine-wide power `RESET` ([DEVICE_CONTRACT](DEVICE_CONTRACT.md#device-reset-g3-specified-as-unavailable)).
- [x] CPU probe: repeated Screen kills (4 mid-frame faults, 5 generations; 3 watchdog-recovered faults), replacement renders, peers unaffected, pins released ([record](../tests/SCREEN_RECOVERY_ACCEPTANCE.md)). Local runs, not an identified bundle.
- [ ] Package the G3 source and CPU runs into an identified acceptance bundle (G6 rules); add `screenrecovery` to the CI profiles.

### G4 Finite lifetimes
- [x] Supervisor-readable remaining lifetime per namespace (2026-10-08; `SYS_LIFETIME` 75, [contract](LIMITS_AND_LATENCY.md#remaining-lifetime-report-g4), [test_lifetime](../tests/test_lifetime.py)).
- [x] Planned-maintenance procedure, with a checked-source test of replacement before `EOVERFLOW` ([procedure](LIMITS_AND_LATENCY.md#operating-limit-and-planned-maintenance)). Source only.
- [x] Decide on a wider versioned reply ABI, or write the operating limit: operating limit written, wider ABI declined.
- [x] CPU run: a supervisor reads the report and replaces a client before exhaustion (2026-10-08, `LAIX_RECOVERY_FIXTURES=lifetime`; replaced with 4095 calls left, replacement in a fresh namespace, no `EOVERFLOW`; [record](../tests/LIFETIME_ACCEPTANCE.md)). Local run, not an identified bundle.
- [ ] Package the G4 source and CPU run into an identified acceptance bundle (G6 rules); add the `lifetime` profile to the CI profiles.

### G5 Kernel latency
- [x] Staged, resumable teardown with interruption/cancellation tests (2026-10-08; `taskReap` commits one dead root per section and the idle loop resumes it after an IRQ window; [test_staged_reap](../tests/test_staged_reap.py), 14 cases: pinned-until-commit, timer IRQ and creation between stages, termination/collect mid-teardown, BUSY-DMA skip; [contract](LIMITS_AND_LATENCY.md#staged-teardown-g5)). CPU: the `latency` probe sees the EXIT section stage exactly one root, then seven idle stages of at most 1,550,591 cycles (12.1 ms), identical at 32 MiB and 128 MiB. Granularity is one root; populate and load are not staged. Local runs, not an identified bundle.
- [x] Tighter latency budget with a regression guard (2026-10-08; budget 64,000,000 → 2,560,000 cycles, 500 ms → 20 ms, against a longest section of 1,859,714 cycles / 14.5 ms and a longest timer gap of 3,177,135; per-kind `KIND_CEILINGS` in the probe, and a source test that keeps the probe, the docs and the other probes in agreement). Eight-task cleanup went from 12,144,655 cycles in one section to 1,555,589 plus seven stages.
- [x] Nesting-safe trap protocol only if a selected workload requires it: decided not required ([TARGET_WORKLOAD](TARGET_WORKLOAD.md), [G5](GAP_05_KERNEL_LATENCY.md#decision-nesting-safe-trap-protocol)); trap entry stays non-nesting and the kernel non-preemptible.
- [x] Race and reuse campaign for staged teardown, as the readiness rules require: source suite plus local CPU reruns on the staged kernel of `soak` (4096 lifetimes, 512 faults), `loader`, `supervisor`, `objects`, `memory`, `sharing`, `recovery`, `lifetime`, `services` with device latency, UART bootstrap/liveness/request-reply, and Screen recovery `watchdog` and `production`. The Screen recovery `scenario` probe fails at its first render marker (`r1` is a pointer, not the generation) with and without staging, so that failure is not caused by G5; it is open and not yet diagnosed.
- [ ] Package the G5 source and CPU runs into an identified acceptance bundle (G6 rules); regenerate the A8 provenance for the new images and budget.

### G7 Application layer
- [x] Writable block path and filesystem service (2026-10-08; [FILESYSTEM](FILESYSTEM.md): kernel WRITE/FLUSH with three-way write authority, Disk sector stage, WFS1 service with atomic commits. Source accepted (2026-10-08; the whole source suite, 595 tests, passes after the G7 work, 132 of them new across the five items) including a power loss at every device event of a scenario in five survival modes. The `fs` CPU profile and [probe](../tests/probe_fs_cpu.py) are written but not run: not built or run on a CPU, not in CI. Not supervised by the recovery profile yet).
- [x] General ELF loader service (2026-10-08; [SHELL](SHELL.md): the Exec service loads a named file from the filesystem through `SYS_TASK_LOAD`, with a limit and a fixed one-endpoint authority for the program. Source accepted, 10 tests; CPU not run).
- [x] Shell application (2026-10-08; [SHELL](SHELL.md): `ls cat write cp mv rm df sync echo run`, line editing, atomic `mv`, over the UART console service. Source accepted, 15 tests plus the boot policy and probe tests; the `shell` profile and its probe are written and never run on a CPU).
- [x] Ethernet driver and protocol stack services (2026-10-08; [NETWORK](NETWORK.md): a kernel broker owns the card's rings and buffers (3 syscalls, `DEVICE_NET`), a driver service moves whole frames, an IP service speaks ARP, IPv4, ICMP echo and UDP, with ping and DNS lookup as its two applications; no TCP. Source accepted, 44 tests including the stack against a gateway model that checks every frame it sends; the `net` profile and its probe are written and never run on a CPU).
- [x] POSIX-like library, if needed: decided not needed yet, with the condition for revisiting it ([G7](GAP_07_APPLICATION_LAYER.md#decision-posix-like-library)).
- [ ] Run the `fs`, `shell` and `net` profiles on a CPU (build with `LAIX_CONSOLE=…`, run the three `probe_*_cpu.py`), add them to the CI matrix, and fold them into an identified acceptance bundle (G6 rules). None has been built or run.
- [ ] Put Fs, Exec, the net driver and the IP service under the recovery supervisor.

## Rules for closing an item

Follow [Keeping evidence current](READINESS_MATRIX.md#keeping-evidence-current):
source regressions plus an identified CPU run, a matrix row update in the same
change, and no reuse of a previous image's result. A gap closed on documents
alone stays "pending" until its evidence exists. Do not propose raising the
128 MiB RAM limit or reopening SMP, which are deliberate machine decisions.

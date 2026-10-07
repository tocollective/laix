# Target workload (G1)

[G1](GAP_01_STATIC_PROFILE.md) · [Limits](LIMITS_AND_LATENCY.md) · [Optional extensions](OPTIONAL_EXTENSIONS.md)

Date: 2026-10-07. Status: **selected and confirmed bound by bound (2026-10-08)**. This
record fixes the workload that G2, G4 and G5 are judged against. It adds no
kernel mechanism. The 2026-10-08 128 MiB latency and 64 KiB Files read results are
local CPU runs outside any bundle. The profile is the one the accepted
campaigns already exercise ([A9 workload table](../tests/ACCEPTANCE_CI.md#current-campaign-and-scope)),
so choosing it raises no bound. If the intended use differs, change the numbers
below first and then re-run the comparison.

## Profile

| Dimension | Target | Source of the number |
| --- | --- | --- |
| Machine | One CPU, one thread per task, 128 MHz | Machine design; `HARTID` reads 0 |
| RAM | **2–128 MiB** for the kernel latency workload (memory/reaping measured at 32 and 128 MiB, 2026-10-08); Screen and the Services/Input/Disk workloads are measured at 2 MiB only and have no claim above it. Below 2 MiB there is no claim | Screen requires 2 MiB (A7); Services/Input/Disk run at 2 MiB and maximum memory/reaping at 32 MiB (A8) |
| Concurrent tasks | At most **five** in a supervised graph (a supervisor with at most four children). Kernel-built fixtures use up to eight | Four child/control records per ordinary creator; the accepted production profile keeps five tasks live (server, client and supervisor images) |
| Resident services | UART, Screen, Input, Disk, Files; Echo as the recovery exemplar | [Simple services](SIMPLE_SERVICES.md), [service recovery](SERVICE_RECOVERY.md) |
| Application images | Compiled M, from the immutable catalog only. Each at most **8 pages (32 KiB)** of `PT_LOAD` memory in at most 3 segments | Measured below |
| Private memory per task | At most **96 frames**, of which heap at most 64 KiB per allocation | Per-task frame quota; `heapAllocate` limit |
| IPC | 0–32-byte messages, one outstanding call per client | Published IPC bound |
| Storage and bulk data | Read-only, 512-byte sectors; Files returns at most 16 bytes per read; Input returns at most 32 events | A7/A8 contracts |
| Screen | Fixed 640×480 console; text and bitmap glyph cache, no windowing | [Screen contract](SCREEN_IRQ_DMA.md) |
| Duty cycle | Session length (hours), boot to power-off. A planned whole-system restart is acceptable maintenance | **Assumption**; drives G4 |
| Latency | Interactive. No hard real-time, priorities or CPU reservation | **Assumption**; drives G5 |
| Out of scope | POSIX layer, shell, networking, writable or persistent filesystem, runtime loading of user programs (the mechanism exists, [FILES_LOADER](FILES_LOADER.md), but the profile does not use it), threads, SMP | [Optional extensions](OPTIONAL_EXTENSIONS.md); deliberate machine decisions | G7 added them as separate boot profiles on 2026-10-08 ([filesystem](FILESYSTEM.md), [shell](SHELL.md), [network](NETWORK.md)); they are not part of the measured workload.

The two assumptions (duty cycle and latency class) are choices, not
measurements. Everything else is read from the tree.

## Comparison with the published bounds

Image sizes come from the `PT_LOAD` headers of the built ELFs in `build/`
(`memsz` summed in pages). The catalog allows 3 segments and 64 pages (256 KiB).

| Image | Segments | Pages | Memory size |
| --- | ---: | ---: | ---: |
| `screen.elf` (largest) | 3 | 7 | 19,516 B |
| `recovery-user/disk`, `echo`, `files` | 3 | 6 each | 15,610 B |
| `recovery-user/policy` | 3 | 6 | 14,624 B |
| `objects.elf`, `files.elf` (services) | 3 | 6 / 5 | 14,332 / 12,490 B |
| `services/application`, `stress-client` | 2 | 4 | 10,776 / 11,612 B |

| Bound | Limit | Target demand | Verdict |
| --- | --- | --- | --- |
| Task slots | 8 (6 ordinary) | 5 in the accepted production profile; 8 in the kernel-built latency fixture | Sufficient |
| Children per creator | 4 | 4 | **At the limit and sufficient**: the profile's graph is a supervisor with four children. A fifth concurrent child is outside the profile; it needs a second supervisor or a larger quota |
| Image pages | 64 | 7 | Sufficient, 9× headroom |
| Image segments | 3 | 3 | At the limit by construction; the linker layout is fixed at code/rodata/data |
| Frames per task | 96 | Image 4–7 plus stacks, tables and startup; heap adds up to 16 per allocation | Sufficient for the accepted graphs; a task that holds several 64 KiB heap regions can reach the quota, which is the documented `ENOMEM` |
| Endpoints / handles | 16 / 16 per task | Five services plus clients | Sufficient |
| IPC payload | 32 B | Fixed 32-byte requests | Sufficient for control traffic, **not for bulk** |
| Bulk read | 16 B per Files read | 64 KiB file = 4,096 reads, **measured 7.1 s** (9.2 KB/s) | Sufficient for 16-byte control reads; **not for bulk**. See below |
| RAM | 2, 32 and 128 MiB measured (kernel latency workload) | 2–128 MiB | Sufficient by evidence for the kernel workload; services and Screen only at 2 MiB |

### Bulk transfer cost

Measured 2026-10-08 (local CPU run, [probe_file_read_cpu.py](../tests/probe_file_read_cpu.py)):
a compiled client reads the first 64 KiB of the Files file in 4,096 requests of
16 bytes, the most Files returns. The span from the client's start to its exit
is **908,672,147 cycles, 7.10 s at 128 MHz**, or 221,844 cycles (1.73 ms) per
read, about 9.2 KB/s. Configuration: 2 MiB RAM, 128 MHz, `--deterministic`, ordinary
Input, Disk and Files services, existing emulator and ROM.

This is about four times the earlier estimate of 1.8 s (4,096 × the 54,768-cycle
call/accept/reply maximum). The estimate counted one IPC round trip. Each Files
read is a client call, then two Disk calls (a medium check, then the sector read),
and the sector read waits for a real DMA completion IRQ, so the span includes
three round trips, the device and idle time between them. Files returns at most
the remainder of one 512-byte sector, so larger reads are never batched.

Consequence: 16-byte reads (the Services application's proof read) are
cheap. Reading files of tens of KiB is slow enough that shared bulk grants, the
entry condition in [OPTIONAL_EXTENSIONS.md](OPTIONAL_EXTENSIONS.md), would apply
if the workload ever did that. The selected profile does not: it has no
filesystem, only the one read-only file.

Reproduce (the build replaces the Services image; rebuild it afterwards):

```sh
LAIX_ACCEPTANCE_FILEREAD=1 LAIX_CONSOLE=services sh laix/build.sh
python3 -B laix/tests/probe_file_read_cpu.py laix/build/services.img laix/build/services.map \
  --emulator "$PWD/bin/wrm081632" --rom "$PWD/bin/firmware.rom"
LAIX_CONSOLE=services sh laix/build.sh
```

## What this decides for the other gaps

- **G2 (policy in the kernel).** The target loads only catalog images. A
  Files-backed loader is not required by the workload; it was implemented anyway
  (2026-10-08, [FILES_LOADER](FILES_LOADER.md)) and the target does not depend on it. G2's data-driven catalog and device descriptors remain valid on their
  own merits, not as workload requirements.
- **G4 (finite lifetimes).** The chosen duty cycle is session length with a
  planned restart. One client calling back to back at the A8 maxima admits about
  2,300 calls per second, which reaches the 8,388,607-call reply limit of one
  slot in roughly an hour. That is an order-of-magnitude estimate, and real calls
  are faster, so the time is shorter. Whether this fits depends on the request
  rate of the chosen applications. G4 (2026-10-08) did both: the
  [operating limit](LIMITS_AND_LATENCY.md#operating-limit-and-planned-maintenance)
  is written and a supervisor can read the remaining lifetime.
- **G5 (kernel latency).** An interactive workload with no real-time class does
  not require a nesting-safe trap protocol, and none was added. Teardown is now
  staged one root per section and the IRQ-disabled budget is 20 ms (2,560,000
  cycles), down from 500 ms; the measured maximum is 14.5 ms (a full
  `SYS_TASK_LOAD`), against 94.9 ms for eight-task cleanup before staging. A
  target tighter than about 15 ms would need smaller populate/load sections,
  which are not staged, and is not claimed.

## Open items before G1 closes

All resolved 2026-10-08. Items 1, 3 and 4 are decisions recorded here; no bound
was raised, so no section budget needed re-measuring beyond item 2.

1. ~~Decide whether the four-children-per-creator quota is enough.~~ Enough: the
   profile is a supervisor with four children. A larger application count is a new
   workload, to be stated first.
2. ~~Add latency acceptance at 128 MiB, or record 32 MiB as the supported ceiling
   in the [readiness matrix](READINESS_MATRIX.md).~~ Measured at 128 MiB (see
   [Timing envelope](LIMITS_AND_LATENCY.md#timing-envelope-and-retained-correctness-baseline)).
3. ~~Measure a 64 KiB file read through Files, or accept the arithmetic above.~~
   Measured: 7.10 s, see Bulk transfer cost.
4. ~~Confirm the duty-cycle and latency assumptions.~~ Adopted as the profile:
   session-length duty with planned restart, interactive latency without a
   real-time class. They are choices, not measurements; change the numbers
   above and re-run the comparison if the intended use differs.

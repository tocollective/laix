# Screen, IRQ and DMA acceptance

Status on 2026-10-04: the eight screen/IRQ/DMA implementation items in
[stage 6](../docs/06_USER_SERVICES.md) are complete. The implementation contract
is [SCREEN_IRQ_DMA.md](../docs/SCREEN_IRQ_DMA.md). The complete source suite
passed 262 tests, including 17 dedicated screen/IRQ/DMA tests. Fourteen screen
CPU cases and eleven default UART regression cases passed against the
existing emulator and firmware. Only LA/IX/user images were built; WRM and
firmware were not rebuilt.

## Source execution evidence

Command from the repository root:

```sh
python3 -B -m unittest discover -s laix/tests -p 'test_*.py'
```

Result: 262 tests, OK, 213.581 seconds. The dedicated file is
[test_screen_services.py](test_screen_services.py). Its 17 checks execute the
M source through the test evaluator or examine user module closures; this
is distinct from CPU/device execution.

| Requirement | Source evidence |
| --- | --- |
| User component separation | Screen, Unicode/font, cache and bitmap closures have no privileged kernel hardware/allocator dependencies; independent entry/startup checks |
| Exclusive bounded NX grants | Every page of the exact VRAM/video/font grant; wrong owner, identity, permission, extent and overflow denied; ordinary map/protect/unmap/copy cannot extend authority |
| Mapping lifecycle | Resource teardown preserves reserved font and VRAM; full bootstrap startup validation and rollback at each of 15 injected mapping failures |
| IRQ notification | Pending before wait, atomic block/wake, matching wait reason, duplicate coalescing, foreign token, timeout, unowned mask and owner death |
| IRQ during IPC | A real IPC wait remains blocked with its notification retained; after reply the matching IRQ wait consumes it |
| Level rearm | Asserted level prevents completion; mask persists, timer ENABLE is preserved; only cleared level permits rearm |
| DMA authority | No user drawing-engine/physical DMA operation; broker accepts only fixed font READ, validated glyph/half and selected owner |
| Physical buffer lifetime | Page-aligned broker-owned physical address, allocator pin rejects free, nonidentity user destination receives only the checked 16-byte completion |
| Cancellation/late completion | Reservation survives task teardown while BUSY; no replacement command; reaper releases only after quiescence |
| DMA failure | Error, changed medium and invalid destinations publish no data; pinned page released only after completion |
| Embedded user images | RX/R/RW construction, zero BSS, segment/range checks and rollback |
| Screen validation | Bounded complete UTF-8, scalar/control checks, invalid input rejected before drawing, complete-byte result count |
| CPU raster algorithm | Bitmap expansion, scroll/upload bounds, canaries and fences in source execution |
| Bitmap/cache protocol | Exact request/reply bounds, generations, both halves staged, failed reply preserves old slot, cache-hit liveness validation |

The fixture models device state and fences; it does not measure real hardware
race timing. The physical buffer is never supplied by a user, so foreign-page
or overflowing arbitrary-DMA requests have no interface.

## CPU/device execution

Screen images were built with `LAIX_CONSOLE=screen sh laix/build.sh`.
Run the probe against those artifacts:

```sh
python3 -B laix/tests/probe_screen_cpu.py \
  laix/build/screen.img laix/build/screen.map \
  --rom laix/build/acceptance/screen-firmware.rom
```

The probe uses 1 MiB RAM and temporary boot disks. It checks all three tasks'
startup/device rights, RX entry, RO/NX startup, all 77 VRAM grant pages, video
RO mapping, exact grant bounds, denied peer aliases and supervisor NX device
aliases before entry. Negative fixtures change only saved user contexts to
existing user store/syscall instructions or an NX address. They do not change
PTEs, generate instructions or program device registers through the monitor.

| CPU case | Observation |
| --- | --- |
| `natural` | Screen/storage run in user mode; banner and Unicode writes succeed, application exits 0; both servers return to blocked accept. Snapshot glyph-cache bytes and the first rendered glyph's pixels match the disk font exactly. Disk DMA, bitmap IPC, video notification wait and rearm execute naturally. |
| `mmio-write` | Screen write through video RO alias faults with CAUSE 10; kernel and bitmap server continue |
| `video-nx`, `vram-nx`, `font-nx` | User instruction fetch faults with CAUSE 8 at the selected alias |
| `font-write` | Screen write to immutable font index faults with CAUSE 10 |
| `client-vram` | Application cannot write the screen's VRAM alias; CAUSE 10 |
| `irq-denied`, `rearm-denied` | Client cannot wait/complete the screen token; syscalls 24/25 return -EPERM |
| `video-denied` | Client has no screen broker operation; syscall 26 returns -EPERM |
| `dma-denied`, `dma-result-denied`, `cancel-denied`, `font-denied` | Client has no font DMA/result/cancel/validate authority; syscalls 27..30 return -EPERM |

All 14 cases passed without kernel panic. Logs and machine-readable hashes
are in `build/acceptance/screen/`, including `results.json` and each case's
`.monitor.txt` and `.uart.txt` files. Input hashes were rechecked after the run.
The final CPU runs used a preserved copy of the existing user-updated firmware
at `build/acceptance/screen-firmware.rom`.

| Screen input | SHA-256 |
| --- | --- |
| `build/screen.img` | `5c0120d8df56152dd8936d4bcd2e1aae6480d7c3c0ac825be15e702935b8e64b` |
| `build/screen.map` | `45205c2c0dcf818629d2cdcc2542203c8dfcf607e5129cea91b701a52115dcb8` |
| Existing `bin/wrm081632` | `5e7d9f7f90ccbc7929b894c118fecbecb04fe58165b16f1843f7412026e2448c` |
| Preserved existing firmware | `cab72f0aa602b35f746555c05289be61cd8271bba11ee4d773c7a625230c9965` |
| `build/services/screen.elf` | `1bff6dbdd0f2917af46d0002f39412081dd1047a40653f3b685255453de94f1c` |
| `build/services/storage.elf` | `e06f95f197e75b0a412164d4c2af18b8a810bf29d804bc6dc5ac4507c8ccbabb` |
| `build/services/application.elf` | `5981631402b70736d0a7c6a8b460044e81732a03695c6e768565745180d98bd8` |

## Default UART regression

Built with `sh laix/build.sh`, then ran:

```sh
python3 -B laix/tests/probe_bootstrap_cpu.py \
  laix/build/laix.img laix/build/laix.map \
  --rom laix/build/acceptance/screen-firmware.rom \
  --log-dir laix/build/acceptance/uart_screen_regression
```

All 11 cases passed: natural banner/blocked server/application exit, denied
client UART/task/MMIO/start-page access, short header, bad version/type/length,
oversized text and bad text. This preserves the default UART protocol.
Logs are in `build/acceptance/uart_screen_regression/`.

| UART input | SHA-256 |
| --- | --- |
| `build/laix.img` | `8d1a1b6b47235b22e7039523a82da147dc8a29d10d71f9485b5dad1b91129498` |
| `build/laix.map` | `3bc843d5eb416f6e301b70a0e89cb5ecf5e443338a174fb12105946165c18d07` |

Emulator and firmware hashes are identical to the screen run.

## Remaining broader acceptance

The three panic/IRQ/DMA safety checklist items have additional acceptance in
[DEVICE_SAFETY_ACCEPTANCE.md](DEVICE_SAFETY_ACCEPTANCE.md), including two
actual CPU panic cases with blocked/dead output services and eight boundary,
ownership and lifetime source checks. The updated screen image passed all
14 screen CPU cases again; its current hashes are recorded with that run.

The original run did not include dedicated multi-client or forced timing
campaigns. [A9](ACCEPTANCE_CI.md) supplies separately identified evidence for:

- Four compiled Screen applications, repeated FIFO writes and timer preemption.
- Forced shared DONE/VBLANK assertions at wait and rearm boundaries, held-level
  masking and subsequent natural application/framebuffer progress.
- Physical disk BUSY at owner death, timeout/cancellation, late completion and
  post-quiescence canaries in the Services profile.
- Real removable-medium replacement/removal during DMA in the Media profile.

Those results belong to A9's bundles, rather than the original hashes above.
Screen replacement between bitmap halves, before cache-hit validation, and
fixed Screen automatic restart remain separate extensions. Explicit runtime
Disk/Files replacement has its own [recovery record](SERVICE_RECOVERY_ACCEPTANCE.md).

The failure CPU cases use an untrusted screen task without unrestricted DMA
MMIO. They prove actual mapping faults and continued kernel/peer execution,
not isolation of a driver granted full physical DMA registers.

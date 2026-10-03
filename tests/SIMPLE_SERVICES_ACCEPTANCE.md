# Simple services and failure acceptance

Status on 2026-10-04: Input, Disk and Files are independent user images, with
bounded protocols and explicit rights/failure behavior. Runtime restart is
unavailable; constructors and grants are sealed. DMA-owner destruction waits
for physical quiescence. The [implementation contract](../docs/SIMPLE_SERVICES.md)
specifies scope and the hardware's lack of a per-disk abort.

Only LA/IX and its user images were built. The existing WRM executable and
firmware ROM were used unchanged.

## Source execution

```sh
python3 -m unittest discover -s laix/tests
```

The dedicated [test_simple_services.py](test_simple_services.py) contains 15
checks. Device registers, FIFO arrival and DMA completion are fixtures; the
checked M AST executes the actual scheduler, MMU, IPC, capability and broker
logic. Source evidence is distinct from real CPU/device execution.

| Requirement | Source evidence |
| --- | --- |
| Startup and rights | Four roles/protocols, exact endpoint/device rights, private RO/NX startup pages, no user MMIO mappings and no kernel imports in user image closures |
| Transactional construction | All 20 mapping failure points roll back pages, roots, grants and endpoints; construction can retry |
| Input | FIFO and HID release bit, sticky hardware/software overflow, bounded draining, zero padding, invalid output consumes no event/status, coalesced IRQ and safe level rearm |
| Disk | Fixed extent, zero/oversized counts, end overflow and cross-sector reads rejected before submission; exact byte copy after completion; physical pin prevents reuse |
| Exit/fault with DMA | Accepted and queued clients receive EPIPE immediately, stale handles/IRQ tokens fail and new submissions are disabled; directory/pages/stack and DMA buffer remain allocated until completion |
| Generations | Existing handle/IRQ tests verify monotonic generations, stale-token rejection, exhaustion without wrapping, owner death and bootstrap sealing |
| Protocols | Malformed/stale client requests do not reach devices or kill services; stat, EOF and bounded short reads; failed/malformed upstream replies publish no bytes |
| Public clients | Header/status/size/generation/count, HID bits and flags checked before output publication |

If BUSY never clears, retaining the pin and the Dead owner's resources is the
required quarantine policy, not successful device recovery. No restart policy
or regrant of a terminal task is claimed.

## CPU and device execution

```sh
LAIX_CONSOLE=services sh laix/build.sh
python3 laix/tests/probe_simple_services_cpu.py \
    laix/build/services.img laix/build/services.map
```

Ten cases passed at 2 MiB RAM:

| Case | Evidence |
| --- | --- |
| `natural` | Four tasks run; input poll and chained file/disk calls succeed; returned 16 bitmap bytes match the appended disk image; app exits successfully; all three services block in accept; DMA buffer released |
| `input-fault` | Actual fetch page fault; application receives failure and exits; Disk/Files continue accepting; Input reaped |
| `disk-fault` | Actual fetch page fault; file failure propagates to app; Files exits according to policy; Input continues; Disk reaped |
| `file-fault` | Actual fetch page fault; app's file endpoint fails; Input/Disk continue accepting; Files reaped |
| `disk-inflight-fault` | File server and app both await accepted replies when the disk broker reserves DMA memory; disk user return is redirected to unmapped zero; fault cancels the chain, both services terminate according to policy, and buffer/resources are eventually reaped |
| `input-denied` | Application syscall 31 returns EPERM in user mode |
| `disk-info-denied` | Application syscall 32 returns EPERM |
| `disk-begin-denied` | Application syscall 33 returns EPERM |
| `disk-finish-denied` | Application syscall 34 returns EPERM |
| `disk-cancel-denied` | Application syscall 35 returns EPERM |

The probe changes saved user return contexts in temporary machines to trigger
faults; it emits no instructions and changes no MMU policy or device registers.
Image, kernel map, emulator, ROM and user ELF hashes are recorded in
`build/acceptance/simple-services/results.json`. Each successful case has UART
and monitor logs alongside that report. None of the user failures causes a
kernel panic.

The headless natural poll observes the device's empty keyboard FIFO. HID event
arrival, overflow, concurrent arrival and keyboard IRQ coalescing are verified
by source/device fixtures, not by host keyboard injection on CPU. The in-flight
CPU case proves an outstanding DMA reservation and safe eventual cleanup; it
does not guarantee BUSY is still set at the exact owner-death instruction. The
source test explicitly holds BUSY through reaping and completes it later.

## Final regression results

The complete source suite passed **277 tests** in **251.304 seconds**, including
all 15 dedicated simple-service tests and the existing 17 screen/IRQ/DMA tests.
The unknown-syscall fixtures now use number 63 because number 31 is allocated
to the input broker. The syscall-set check includes the five new operations.

All **25 CPU cases** run for this change passed: the ten simple-service cases
above, four focused screen regressions (`natural`, `mmio-write`, `dma-denied`,
`irq-denied`) and all eleven UART bootstrap cases. LA/IX UART, screen and simple
service profiles built successfully using the existing toolchain. Shell syntax
and `git diff --check` also passed. No WRM or firmware build was performed.

The focused screen report was regenerated for these four cases; the historical
14-case acceptance in [SCREEN_IRQ_DMA_ACCEPTANCE.md](SCREEN_IRQ_DMA_ACCEPTANCE.md)
remains separate evidence from its earlier run.

# Panic, IRQ and DMA safety acceptance

Validated on 2026-10-04. The three safety items in
[stage 6](../docs/06_USER_SERVICES.md) are complete at the evidence levels below.
WRM and firmware were not built or modified. Only LA/IX and its user images
were rebuilt after the IRQ corrections.

| Requirement | Checked-source execution | Actual CPU/device execution |
| --- | --- | --- |
| Panic survives output-service failure | Panic's module closure contains only trap layout, definitions and direct UART formatting | Blocked accept and dead/reaped screen server: one full supervisor panic dump, exact EPC/STATUS/BADADDR/PTBR/FCSR and all 32 GPRs, stage, exit 254, no double fault |
| IRQ wakeup, coalescing and ownership | Level at atomic block; assertion between pending check and enable and at enable; held level denies repeated rearm; no duplicate notification after wake; a second owned line cannot complete the selected wait; timeout still wakes the selected wait; foreign/stale/malformed tokens leave grants and masks unchanged | All 14 existing screen cases pass, including natural video/disk IRQ wait/rearm and foreign wait/complete denial |
| DMA ownership, bounds and lifetime | Foreign PTE backing frame, partially valid output span and wrapping destination rejected without partial writes; zero/oversize/cross-sector/out-of-extent/wrapping reads issue no command; last valid byte succeeds; early finish and repeated reap preserve the BUSY page pin; late completion releases once | Natural disk read/copy succeeds; foreign begin/finish/cancel/validate authority is denied; simple-service natural boot and injected disk fault with an outstanding reservation pass, including final DMA cleanup |

## IRQ corrections

Two new tests failed against the previous source:

- Repeated `irqNotify` after waking a blocked owner recreated `pending`, so a
  second wait incorrectly succeeded before servicing/rearming the line.
- A task granted two lines could be awakened by the other line, because
  `WAIT_IRQ` alone did not identify the selected subscription.

`irqNotify` now coalesces notifications throughout the in-service interval.
Each grant records whether its own line has a waiter; notification and timeout
wake only that waiter. Successful wake, rearm and revocation clear the flag.
Mask/fence ordering, level validation and unrelated PIC ENABLE bits are retained.

## Reproduce

From the repository root:

```sh
python3 -B -m unittest discover -s laix/tests -p 'test_device_safety.py'
python3 -B -m unittest discover -s laix/tests -p 'test_*.py'
LAIX_CONSOLE=screen sh laix/build.sh
python3 -B laix/tests/probe_service_panic_cpu.py \
  laix/build/screen.img laix/build/screen.map \
  --rom laix/build/acceptance/screen-firmware.rom
python3 -B laix/tests/probe_screen_cpu.py \
  laix/build/screen.img laix/build/screen.map \
  --rom laix/build/acceptance/screen-firmware.rom \
  --log-dir laix/build/acceptance/device-safety-screen
LAIX_CONSOLE=services sh laix/build.sh
python3 -B laix/tests/probe_simple_services_cpu.py \
  laix/build/services.img laix/build/services.map \
  --rom laix/build/acceptance/screen-firmware.rom \
  --log-dir laix/build/acceptance/device-safety-simple \
  --case natural --case disk-inflight-fault
```

Results: all 285 source tests passed in 249.040 seconds, including eight new
device safety tests. Two panic CPU cases, 14 screen CPU cases and two
simple-service CPU cases passed. Panic logs and input hashes are in `build/acceptance/service-panic/`;
screen regression logs and hashes are in `build/acceptance/device-safety-screen/`.
The simple-service run is in `build/acceptance/device-safety-simple/`.
All CPU probes recheck input hashes after execution.

| Shared CPU input | SHA-256 |
| --- | --- |
| `build/screen.img` | `a8b3939fd9bc839cd409bf707810cf6a2a9839463759c0e3d045ea0e6f49a7d5` |
| `build/screen.map` | `372a50665fe9277bafa3dfb801483a66fa84fb2d0e5cc5e8fb18721d2505401c` |
| Existing `bin/wrm081632` | `5e7d9f7f90ccbc7929b894c118fecbecb04fe58165b16f1843f7412026e2448c` |
| Preserved existing firmware | `cab72f0aa602b35f746555c05289be61cd8271bba11ee4d773c7a625230c9965` |

The panic probe waits for real supervisor idle timer entry, changes only the
selected saved return frame to an existing supervisor BREAK and executes the
real IRET/trap/panic paths. It never asks an output server to print the dump.
The dead-server case checks resource reaping before injecting the kernel fault.

Source boundary tests execute the checked M AST with level-PIC/DMA fixtures;
they do not measure real hardware timing or replace the broader dedicated CPU
stress work listed in [screen acceptance](SCREEN_IRQ_DMA_ACCEPTANCE.md).
User DMA never accepts a physical page/address: the trusted broker selects and
pins its own page. Foreign user backing pages are tested at the checked copy
boundary. A stuck device remains quarantined until reset; the safety contract
does not promise successful completion of a stuck transfer.

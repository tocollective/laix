# G3 supervised display recovery acceptance

Date: 2026-10-08. Scope: runtime regrant of the display and the font-extent
reader after owner death, Screen in the supervised recovery policy, explicit
client reconnection. WRM and firmware were **not built**; CPU runs use the
existing `bin/wrm081632` and `bin/firmware.rom` bytes. Only LA/IX images were
compiled.

The [contract](../docs/SERVICE_RECOVERY.md#supervised-display-screen-and-bitmap-storage),
[checked-source tests](test_screen_recovery.py), [CPU probe](probe_screen_recovery_cpu.py)
and build provenance (`laix/build/screenrecovery.provenance.json`, written by
[screen_recovery_provenance.py](../tools/screen_recovery_provenance.py)) establish
the evidence below. The three CPU modes are CI profiles (`screenrecovery`,
`screenrecovery-watchdog`, `screenrecovery-production`) and passed from identified
bundles in the [G7 packaging campaign](ACCEPTANCE_CI.md#g7-packaging-campaign-2026-10-08)
(2026-10-08); no remote run. The `scenario` probe's earlier failure was a probe
defect (breakpoints by virtual address in images that share a load address); the
probe now filters by caller and by the published Screen task.

## Implementation evidence

| Requirement | Implementation and verification |
| --- | --- |
| Regrant Screen resources to a replacement after owner death | `taskRuntimeDevices(child, SCREEN)` calls `screenDevicesCheck`, issues the video IRQ and `screenDevicesRegrant`, which installs exactly the Screen role's device-table rows through `mmuInstallResource` (the boot-only `mmuGrantResource` stays sealed). Source: exact leaves and permissions, owner-only `screenControl`, foreign and dead-owner rejection |
| Same quiescence preflight as Disk | No live owner; every role range free in the resource ledger (a dead owner's directory still holds it, so reclamation is observable); video engine not BUSY; fixed bootstrap display never regranted. Source: refusal while alive, repeated refusal while dead-but-unreaped with no IRQ, rights or ledger change, success after reap with a fresh IRQ generation, BUSY refusal, death stops scanout |
| Screen has no DMA pin | Screen holds no DMA register or command; it writes VRAM with CPU stores. Nothing to wait on beyond reclamation. The font-extent reader keeps the Disk rules (pin until BUSY clears): source test holds a pin across owner death and four reaps |
| Font reader replaceable while Screen lives | `FONT` shares the Disk broker path. Found by test: a disk-only init once cleared the runtime display owner; now only a display init assigns it |
| Screen in the recovery policy | `recoverChain` generalizes Files/Disk (`recoverFilesDisk` is a wrapper); `recoverScreenBitmap` names Screen 1/image 4 and storage 2/image 3. Policy fixtures check retire order, creation order, device masks, dependency and generation, publication order and exhaustion |
| Bounded attempts and backoff | Unchanged: five constructions per boot, 1/2/4/8 s |
| Explicit client reconnection | Production client reports the failed generation, closes the handle and resolves again; scenario client keeps the stale handle until the fresh one is installed and checks `-EPIPE` on it |
| No partial grant | Mapping exhaustion after the preflight leaves no owner, IRQ or rights; `serviceConfigure` requires the child's own video IRQ and a complete mapping set |
| Resources return to baseline | Source: eight replacements, exact free-page baseline, zero ledger rows, IRQ owners and extra controls after each. CPU: zero endpoints, controls, registry rows, IRQ owners, ledger rows, DMA pin and display owner |

## CPU evidence

| Image | Result and boundary |
| --- | --- |
| Scenario (`LAIX_SCREEN_RECOVERY_FIXTURES=scenario`) | Five Screen generations on fresh bitmap storage. A fixture Screen renders a glyph and faults before its frame wait and reply, four real faults, with the client's reply right held and VBLANK left asserted. The client keeps the dead generation's handle until the fresh one is installed and sees `-EPIPE` on it. Each generation's frame (read from a machine snapshot, since the monitor cannot read VRAM) holds the identical letter cell, a distinct digit cell and no stale pixels. Echo and a CPU-bound peer outlive every generation. Video and disk IRQ generation 5 each; 14 completions, 4 faults, none quarantined; zero final endpoints, controls, registry rows, IRQ owners, ledger rows, DMA pin and display owner |
| Watchdog (`=watchdog`) | The unmodified production supervisor and client against a Screen that really faults on its third request in generations 1-3. Four generations are observed, each faulting one serving two requests, the fourth serving three; the replacement draws; all five tasks are live; six completions, three faults, no DMA pin |
| Production (unset) | The production supervisor, Screen, bitmap storage, Echo and client boot; the status line is drawn; five live tasks and no completion records. A smoke test, not production fault injection |

Fault injection is by crash fixtures compiled into the Screen (and, in the
scenario image, the supervisor) only; production Screen has no crash command. No
kernel or user instruction was patched.

## Boundaries

- Fixed UART and fixed `screen` bootstraps are unchanged and unrestarted. UART is
  documented as fixed by decision ([G3](../docs/GAP_03_FIXED_SERVICE_RESTART.md)).
- No device reset exists in WRM; none is claimed
  ([specification](../docs/DEVICE_CONTRACT.md#device-reset-g3-specified-as-unavailable)).
  Indefinite BUSY on the font reader's disk remains quarantine, as before.
- A Screen failure replaces bitmap storage too (shared generation).
- Screen medium swap between bitmap halves and cache-hit validation still have no
  CPU campaign.

## Reproduce

```sh
LAIX_SCREEN_RECOVERY_FIXTURES=scenario LAIX_CONSOLE=screenrecovery sh laix/build.sh
python3 laix/tests/probe_screen_recovery_cpu.py laix/build/screenrecovery.img laix/build/screenrecovery.map
LAIX_SCREEN_RECOVERY_FIXTURES=watchdog LAIX_CONSOLE=screenrecovery sh laix/build.sh
python3 laix/tests/probe_screen_recovery_cpu.py laix/build/screenrecovery.img laix/build/screenrecovery.map --watchdog
LAIX_CONSOLE=screenrecovery sh laix/build.sh
python3 laix/tests/probe_screen_recovery_cpu.py laix/build/screenrecovery.img laix/build/screenrecovery.map --production
```

Each probe checks its image's build provenance against the current sources before
and after the run, so rebuild after any source change.

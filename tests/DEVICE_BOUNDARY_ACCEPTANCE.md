# A7 device boundary acceptance

Date: 2026-10-05. All items in
[A7](../docs/AUDIT_07_DEVICE_BOUNDARY.md) are implemented within the read-only
contract below. WRM and firmware were neither built nor modified. LA/IX and
its user images were rebuilt. Checked boxes do not imply disk writes, flush,
a filesystem, networking or general user DMA access.

## Requirement evidence

| A7 implementation requirement | Implementation / validation |
| --- | --- |
| Inventory every register and command | [Complete register/command authority table](../docs/DEVICE_CONTRACT.md#register-and-command-authority-inventory); `test_register_inventory_covers_every_hardware_header_register` compares every header register name |
| Document the trusted computing boundary | [Trust boundary](../docs/DEVICE_CONTRACT.md#trust-boundary), including physical DMA bypass and mapping decisions |
| Replace font-derived storage policy | `DeviceExtent`, `deviceExtentInit`, build-issued `storage-extent.bin`; arbitrary-resource test corrupts LAF and still reads an independently approved sector |
| Keep paths, formats and requests outside mechanisms | User Font/Disk/Files protocol handlers; generic trap INFO/SUBMIT/FINISH/CANCEL family; closure test excludes parsers/renderers |
| Bounded operations and immutable identities | `DeviceOperation` owner, instance and resource generation; stale-instance, command-bit, range/overflow and exhaustion tests |
| Trusted buffers, pins, fences and quiescence | Kernel-only allocator-owned page; allocator reference tests, source/device canaries and natural physical disk DMA |
| Generic IRQ ownership/completion | Existing generation-bearing per-line grants; selected-line/coalescing/rearm race tests; shared video cause test and CPU video/disk IRQs |
| Input/display policy placement | Raw 1–32-event broker; software queue/overflow/four-event replies in user Input; bounded register writes, mode/palette/scanout and rearm sequence in user Screen |
| Logical/physical cancellation | Instance-bound revocation, BUSY quarantine, exactly-once release, no unsupported abort; early finish/death/timeout/late completion tests |
| Approved regrant / dead-owner rejection | Runtime manager factory + child CONFIGURE, subextent selection, exclusive quiescent preflight before IRQ issue; stale owner and changed-medium denial |
| Exclude legacy regression linkage | Normal UART, Screen, Services and Recovery maps exclude selected rendering/font modules; explicit console regression mains retain their dependency closure; independent emergency UART CPU checks |
| Publish generic contract first | [DEVICE_CONTRACT.md](../docs/DEVICE_CONTRACT.md), including ABI migration and requirements for future engines |

| A7 acceptance requirement | Evidence and level |
| --- | --- |
| User policy changes an allowed extent or display configuration | Checked-source manager selects `(512, 96)`; actual CPU production manager configuration selects the same window and the Files reply matches those physical image bytes; source display test changes mode, palette and START without changing the kernel |
| Foreign authority, PA injection, overflow, crossing and unsafe flags denied | Checked-source tests prove no MMIO submission or allocation; CPU denies client INFO/SUBMIT/FINISH/CANCEL, video writes and IRQ operations, with MMU RO/NX checks |
| Exactly-once completion and reference conservation | Checked-source success/error/invalid-copy, repeated finish/reap, stale instance, timeout and death; CPU natural completion, owner fault cleanup, five recovery generations and cancellation pin checks |
| CPU/device canaries preserve quarantined buffers | `disk-cancel-canary` invokes the real user CANCEL syscall during physical DMA, observes logical cancellation + BUSY with broker owner/reference intact, checks two unused-page canaries at physical release and zero references after return; source test also verifies the entire late DMA payload and permanently held pins |
| Shared causes and held levels | Checked-source simultaneous video DONE/VBLANK requires clearing both causes; held level and rearm races preserve masks/wakeups. CPU tests real video/disk lines; user Screen acknowledgement/rearm retries are bounded |
| Regression modules excluded / UART independent | Normal map inspection and closure checks; blocked/dead Screen CPU panic cases verify all registers and direct UART output |
| Unrelated services survive failure/death/stuck engine | CPU input/disk/file fault cases and recovery peer/reconnection; source permanent-BUSY fixture completes unrelated IPC repeatedly while pins and dead owner remain quarantined |

## Results and provenance

The complete source suite passed **381 tests** in **567.476 seconds**. After
final idle/rearm changes, the A7 suite passed **14 tests**, the idle suite passed
**7 tests**, the bootstrap suite passed **9 tests**, and the Screen source
suite passed **17 tests**. The source evaluator
executes type-checked M logic, allocator records, MMU checks and device fixtures;
it does not establish hardware timing or actual instruction execution.

CPU acceptance passed 14 Screen cases, 11 Services cases, two emergency-UART
panic cases, one production subextent case and one five-generation recovery
case (29 runs). CPU/device results and immutable hashes are recorded in
[DEVICE_BOUNDARY_PROVENANCE.json](DEVICE_BOUNDARY_PROVENANCE.json). Raw CPU
logs/reports are under `build/acceptance/device-boundary-*`. Source/compiler
inputs and existing emulator/ROM hashes are included in the record. CPU probes
recheck their inputs after execution; Recovery also binds user/kernel/compiler
sources to its build provenance.

The current Screen acceptance uses **2 MiB RAM**. The revised image failed
bootstrap at 1 MiB; this record does not claim a 1 MiB working Screen image.
The cancellation canary case uses a 128 MHz CPU clock to observe a physical
in-flight operation; other Screen/Services cases use the normal clock. The
monitor changes saved user contexts, unused bytes in the pinned page and the
user manager's configuration fields. It never programs physical device MMIO
or substitutes DMA commands.

## Reproduce

From the repository root, using the existing WRM binary and preserved ROM:

```sh
python3 -B -m unittest discover -s laix/tests -p 'test_*.py'
LAIX_CONSOLE=screen sh laix/build.sh
python3 -B laix/tests/probe_screen_cpu.py \
  laix/build/screen.img laix/build/screen.map \
  --rom laix/build/acceptance/screen-firmware.rom \
  --log-dir laix/build/acceptance/device-boundary-screen
python3 -B laix/tests/probe_service_panic_cpu.py \
  laix/build/screen.img laix/build/screen.map \
  --rom laix/build/acceptance/screen-firmware.rom \
  --log-dir laix/build/acceptance/device-boundary-panic
LAIX_CONSOLE=services sh laix/build.sh
python3 -B laix/tests/probe_simple_services_cpu.py \
  laix/build/services.img laix/build/services.map \
  --rom laix/build/acceptance/screen-firmware.rom \
  --log-dir laix/build/acceptance/device-boundary-services
LAIX_CONSOLE=recovery sh laix/build.sh
python3 -B laix/tests/probe_service_recovery_cpu.py \
  laix/build/recovery.img laix/build/recovery.map --production --window 512 96 \
  --rom laix/build/acceptance/screen-firmware.rom \
  --log-dir laix/build/acceptance/device-boundary-policy
LAIX_CONSOLE=recovery LAIX_RECOVERY_FIXTURES=1 sh laix/build.sh
python3 -B laix/tests/probe_service_recovery_cpu.py \
  laix/build/recovery.img laix/build/recovery.map \
  --rom laix/build/acceptance/screen-firmware.rom \
  --log-dir laix/build/acceptance/device-boundary-recovery
sh laix/build.sh
```

## Limits

One selected storage engine and one operation are supported per boot. Root
approval and Screen/VRAM issuance remain bootstrap policy; runtime managers may
select/regrant only approved storage subranges. Medium replacement invalidates
the approved root for this boot. Read-only RO catalog validation in the MMU
remains, without font-format parsing in device mechanisms. Generic transfer
capacity is one sector; service IPC replies remain at most 16 payload bytes.

A permanently stuck physical device is covered by checked-source fixtures;
this work does not claim a physical stuck-device/reset CPU campaign. Simultaneous
shared IRQ causes and boundary races likewise have source-fixture evidence in
addition to natural real-device CPU IRQ tests. No read-write MMIO grant, disk
write/flush, persistent filesystem, network/audio/shared-folder DMA or cursor
sprite authority is introduced. Future operations must extend the published
range, ownership, completion and resource-accounting contract first.

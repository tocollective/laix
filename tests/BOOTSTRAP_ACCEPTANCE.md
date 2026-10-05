# Bootstrap acceptance

This is a historical bootstrap run. Later Screen/device containment, compiled
M helper execution and explicit runtime recovery are tracked in
[the readiness matrix](../docs/READINESS_MATRIX.md) and [A9](ACCEPTANCE_CI.md).
The original image hashes and exclusions below are unchanged.

This is the historical protocol-1 smoke record. Current normal boot and the
text-console protocol are covered by [console acceptance](CONSOLE_SERVICE_ACCEPTANCE.md).

Validated on 2026-10-04. LA/IX was compiled and linked with the existing M
toolchain. The existing `bin/wrm081632` executable and `bin/firmware.rom` were
used unchanged; WRM and firmware were not rebuilt.

Source checks:

All 239 source/unit checks pass, including nine bootstrap tests with bounded
failure/rollback scenarios. Type checking and shell syntax checks also pass.

```sh
python3 -B mc/mc.py --check laix/src/kernel/main.m
python3 -B -m unittest discover -s laix/tests -p 'test_bootstrap.py'
python3 -B -m unittest discover -s laix/tests -p 'test_*.py'
sh -n laix/build.sh
```

The bootstrap checks execute the checked init, task, memory, MMU, endpoint,
IPC and syscall ASTs with synthetic addresses/image words. They verify the
startup layout, distinct resources, exact receive/send rights, immutable user
startup mapping, UART denial, call/reply blocking, last-receiver revocation,
invalid records/handles/images, pre-entry authority sealing, all eight task
mapping failures, all twenty init OOM boundaries, endpoint/handle issuance
failures and retry without leaks or runnable partial tasks.

CPU acceptance on a ready LA/IX image:

```sh
python3 -B laix/tests/probe_bootstrap_cpu.py \
    laix/build/laix.img laix/build/laix.map
```

| Case | Observed result |
| --- | --- |
| Natural boot | Unchanged init loads two distinct images and exact resource tables. Both enter UM through their own roots. Server blocks before client entry; client sends `B\n` through two calls and exits 0. Server waits in accept and idle runs. |
| UART denied | Client executes the existing SYSCALL instruction with number 0; returns `-EPERM` in UM and the denied byte never reaches UART. |
| Task API denied | Client uses unknown syscall 24; returns `-ENOSYS` in UM. No task-control API is exposed. |
| MMIO denied | Client executes an existing load against UART MMIO; load page fault, cause 9 and correct BADADDR. Only that task dies; server remains Blocked/live and idle runs. |
| Startup store denied | Client executes an existing store against its startup page; store page fault, cause 10 and correct BADADDR. Only that task dies; server remains Blocked/live and idle runs. |

The negative CPU cases edit initial saved user contexts only in temporary
machines, targeting instructions already present in the client image. They
do not emit instructions or modify input image/map/emulator/ROM files. Each
case inspects actual task frames, code copies, mappings and remaining handles;
the probe checks input hashes after completion. Logs and `results.json` are
written to `laix/build/acceptance/bootstrap/`.

The updated scheduler probe also passes `natural`, `regressions` and `exit`
against this image. It validates the new normal boot and preserves the
legacy fixture checks for returning syscall registers/FCSR, supervisor trap
self-tests and task cleanup. Its logs are in
`laix/build/acceptance/bootstrap_scheduler/`.

These checks accept the [embedded startup contract](../docs/BOOTSTRAP.md).
They do not accept the full text-console service, M helper execution, screen
migration, service restart, device IRQ delivery or DMA isolation.

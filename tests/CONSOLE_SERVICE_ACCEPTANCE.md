# Console service acceptance

Validated on 2026-10-04. The implementation covers the eight first-console
items in [stage 6](../docs/06_USER_SERVICES.md); its normative contract is
[User UART console service](../docs/CONSOLE_SERVICE.md).

LA/IX was compiled and linked with the existing M toolchain. The existing
`bin/wrm081632` and `bin/firmware.rom` were used unchanged. WRM and firmware
were not rebuilt.

## Source checks

All 245 checks pass, including six new console service checks. The new checks
parse and execute the actual server and public client helper assembly without
emitting code. They use strict byte-addressed reads to check short headers,
exact bounds, stale bytes beyond delivery, malformed fields/text, zero/maximal
payloads, validation before output, response size/status/count validation,
transport error propagation, bounded private stack storage and the M ABI.
Three clients enqueue in order 3, 2, 4 and receive separate replies through
the checked kernel IPC/MMU/scheduler source; actual parsed server logic outputs
complete messages in that order, with timer-driven scheduler rotations.
Kernel main's module closure excludes screen/font code, and panic's closure
excludes IPC, service, disk and user memory.

```sh
python3 -B mc/mc.py --check laix/src/kernel/main.m
python3 -B -m unittest discover -s laix/tests -p 'test_*.py'
sh -n laix/build.sh
```

These are source execution checks. The multiple-client scenario does not
replace dedicated CPU acceptance of multiple concurrent application images.

## CPU checks

```sh
sh laix/build.sh
python3 -B laix/tests/probe_bootstrap_cpu.py \
    laix/build/laix.img laix/build/laix.map
python3 -B laix/tests/probe_scheduler_cpu.py \
    laix/build/laix.img laix/build/laix.map \
    --case natural --case regressions --case exit \
    --log-dir laix/build/acceptance/console_scheduler
```

The bootstrap probe passes eleven cases:

| Case | Observed result |
| --- | --- |
| Natural boot | Both isolated images enter user mode. Server waits Blocked before client entry. The application executes the same public `consoleWrite` M ABI helper, prints its entire banner through one call, validates the result and exits 0. Server returns to Blocked and idle runs. |
| UART denied | Client syscall 0 returns `-EPERM`; no denied byte is output. |
| Task API denied | Unknown task-control syscall returns `-ENOSYS`. |
| MMIO denied | UART load faults only the client; server remains live/Blocked. |
| Startup store denied | A write to RO startup memory faults only the client. |
| Short header | Two-byte request returns `-EINVAL`, zero written. |
| Bad version | Returns `-EINVAL`, zero written. |
| Bad type | Returns `-EINVAL`, zero written. |
| Bad length | Inconsistent delivered/declared size returns `-EINVAL`, zero written. |
| Oversize text | Declared length 255 returns `-EMSGSIZE`, zero written. |
| Bad text | Printable prefix followed by ESC returns `-EINVAL`; no prefix reaches UART. |

Each invalid request receives exactly the 12-byte protocol response in user
memory, and the server returns to Blocked accept without a kernel panic.
Negative fixtures modify saved contexts and data only in temporary machines,
using existing instructions. Task identity is checked at breakpoints because
both images use the same virtual code addresses. No probe emits instructions
or modifies input artifacts; input hashes are checked after completion.

The scheduler probe also passes `natural`, `regressions` and `exit` against
this image. Logs are in `build/acceptance/bootstrap/` and
`build/acceptance/console_scheduler/`.

| Artifact | SHA-256 |
| --- | --- |
| LA/IX image | `e91097b121c4bf92f3f57a8e54e8eba569f42fd97b9b78263f4d1a3ba2fd7689` |
| LA/IX map | `1f7ae0053cb77e8c59563ebe543531acf03145f3d7f4cac84cf9972c14d3f12d` |
| Existing WRM executable | `5e7d9f7f90ccbc7929b894c118fecbecb04fe58165b16f1843f7412026e2448c` |
| Existing firmware ROM | `4e42ef9742fa0b70efca1c8a113482770dda9ce68b113bcf001942a0ede0d0b5` |

## Remaining acceptance

The original run left dedicated multi-client CPU ordering/stress open.
[A9](ACCEPTANCE_CI.md) now supplies a separate identified four-application,
128-round UART campaign. General service-fault containment, explicit runtime
Disk/Files recovery and independent emergency UART have later evidence in
[the readiness matrix](../docs/READINESS_MATRIX.md). A console-specific
replacement policy remains unsupported by the fixed UART bootstrap. Existing request/reply acceptance
covers general service-fault cancellation and continued scheduling; it is
not evidence for a new console-specific failure probe. Screen/font migration,
IRQ delivery and DMA policy are separate milestones and are not claimed here.

# Finite-lifetime acceptance (G4)

Status: **source accepted; CPU accepted in the G7 packaging campaign, 2026-10-08.** The
`lifetime` profile ran from an identified bundle ([G6](../docs/GAP_06_EVIDENCE_AND_CI.md),
[campaign](ACCEPTANCE_CI.md#g7-packaging-campaign-2026-10-08)); no remote run.

Contract: [LIMITS_AND_LATENCY.md](../docs/LIMITS_AND_LATENCY.md#remaining-lifetime-report-g4).
Source: [test_lifetime](test_lifetime.py) (14 cases). CPU: [probe](probe_lifetime_cpu.py)
with the scenario in [tests/programs/lifetime](programs/lifetime/policy.m).

## What the CPU run proves

1. The scenario supervisor creates a client and stops before publishing it. The
   probe writes the client's reply counter to `0x7fffff - 4098`, which leaves 4098
   admitted calls, two above `LIFETIME_REPLY_RESERVE`. User mode cannot do this
   and the kernel never sets a counter, so the monitor does it once.
2. The client calls back to back on a Service endpoint. The supervisor answers,
   reads `SYS_LIFETIME` for the client on every pass, and sees it due once the
   remaining calls are at most 4096.
3. `retireClient` terminates, reclaims and collects it. The replacement must get
   a different slot, read as not due, and be served at least 20 calls.
4. The client treats `EOVERFLOW` as failure (exit 60), so a pass also means it
   never saw one. The old slot's counter must not change afterwards.
5. At the end: supervisor exit 0, no live children, no leaked endpoint, control,
   service or IRQ rows, three completions (two terminated, none faulted).

## Reproduce

Do not reuse an image from another profile. The provenance file records
`fixtures: "lifetime"`, which the older recovery probes reject.

```sh
LAIX_RECOVERY_FIXTURES=lifetime LAIX_CONSOLE=recovery sh laix/build.sh
python3 -B laix/tests/probe_lifetime_cpu.py laix/build/recovery.img laix/build/recovery.map
```

Logs are written to `laix/build/acceptance/lifetime`. Afterwards rebuild the
other recovery profiles, which share `build/recovery.*`:

```sh
LAIX_CONSOLE=recovery sh laix/build.sh                       # production smoke
LAIX_RECOVERY_FIXTURES=1 LAIX_CONSOLE=recovery sh laix/build.sh   # service faults
```

Because syscall 75 and the construction order change the kernel, every other
CPU profile needed a fresh run (G6 rules); the G7 campaign reran all 23.

## Result

`PASS lifetime CPU: client replaced inside the reserve, replacement served from a
fresh namespace`, on the existing `bin/wrm081632` and `bin/firmware.rom`.

| Item | Value |
|---|---|
| Client seeded with | 4098 calls left (reserve is 4096) |
| Left at retirement | **4095**: three calls were admitted, one after the supervisor's due threshold was crossed and before it retired the client. The reserve absorbed it |
| `EOVERFLOW` seen by a client | none (exit 60 never taken) |
| Client slot / replacement slot | 2 / 3, so the replacement left the nearly spent namespace |
| Replacement calls admitted | 20, from a counter that started at zero |
| Old slot counter after replacement | unchanged |
| Live children, leaked endpoint/control/service/IRQ rows | none |
| Completions | three; two terminated, none faulted |
| image `recovery.img` | `bb1676d6d6f2ee95ae3f44069b9cc28e8583bc639c773c27fa647d44538320f8` |
| map `recovery.map` | `ee27e363055d3850258ac132399da8f590a63a69dfddf69e032727fe4a556a9c` |
| emulator `bin/wrm081632` | `3425ae5ec6000cdfcb9356374658a790435663765c6159d9f1c7caba89e1c90d` |
| ROM `bin/firmware.rom` | `cab72f0aa602b35f746555c05289be61cd8271bba11ee4d773c7a625230c9965` |

The source and artifact manifest is in `results.json`. Editing any `.m`, `.asm`,
`.inc` or `.py` file under `laix/` or `mc/` after this run makes the record stale.
The build printed one new warning, an unused `LIFETIME_REPLY_RESERVE` import in
`user/recovery/policy.m`. It is harmless and was left so that this record still
matches the tree; remove it with the next rebuild and rerun.

## Not covered

- No pool-exhaustion case on CPU: the fallback to a nearly spent namespace is
  checked in source only.
- A local run, not an identified bundle; other CPU profiles are not rerun on this kernel.
- The production supervisors still do not read the report.
- Rates in the operating limit are arithmetic, not measured.

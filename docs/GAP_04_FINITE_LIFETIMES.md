# G4. Identity and reply lifetimes are finite

[Gap checklist](GAPS_CHECKLIST.md) · [Previous](GAP_03_FIXED_SERVICE_RESTART.md) · [Next](GAP_05_KERNEL_LATENCY.md)

Date: 2026-10-07. Priority: **P2**.

## Criterion

Generations never wrap, so a long-running system eventually retires slots.
Retirement is safe but bounds the boot lifetime.

## Current state

Revised 2026-10-08. Contract: [LIMITS_AND_LATENCY.md](LIMITS_AND_LATENCY.md#remaining-lifetime-report-g4).
Evidence: [test_lifetime](../tests/test_lifetime.py); CPU: [record](../tests/LIFETIME_ACCEPTANCE.md).

Unchanged limits:

- Reply identity per task slot ends at 8,388,607 admitted calls; later calls
  return `EOVERFLOW`. Eight namespaces admit at most 67,108,856 calls combined,
  and two slots are reserved.
- Task references, handle slots, transfer tickets, IRQ lines, memory rows and
  endpoint rows retire at their last generation. When no eligible slot is left,
  creation returns `ENFILE`.
- Tokens are boot-local and no counter is ever reset at runtime.

What changed:

- **Readable remaining lifetime.** `SYS_LIFETIME` (75) returns a 44-byte
  `LifetimeReport` to a supervisor with creation authority: the selected
  child's remaining reply calls, task-reference and handle generations, the
  boot total over the namespaces the caller can construct into, and counts of
  retired task slots, handle slots, endpoint rows and IRQ lines. It is read-only.
- **Replacement lands somewhere useful.** The old rule took the lowest free slot,
  so replacing a client near its limit reused the same nearly spent namespace.
  Construction now takes namespaces with more than `LIFETIME_REPLY_RESERVE` (4096)
  calls left first and falls back to the old rule only when nothing else is
  eligible. Below that reserve nothing else changes: slot order is as before.
- **Planned maintenance.** `lifetimeDue` and `retireClient` in the recovery
  policy, and a five-step procedure plus a whole-system restart procedure in the
  limits contract. The replacement is checked too, so a supervisor stops
  replacing when no fresh namespace is left instead of looping.
- **Decision: the reply ABI stays 23 bits.** The written operating limit is the
  contract (50,331,642 calls per boot for an ordinary supervisor, 67,108,856
  with the recovery slots). A wider identity would change the
  `generation << 8 | slot` token and need a versioned ABI and migration of every
  user helper. The target workload is hours-long sessions with a planned restart,
  which fits unless a client calls back to back (about 2,300 calls per second
  exhausts six namespaces in roughly six hours).

## Remaining limits

- **The CPU run is packaged, not remote.** The `lifetime` profile ran in the G7
  campaign ([record](../tests/LIFETIME_ACCEPTANCE.md)) and is in the CI matrix;
  all other profiles were rerun on the same kernel there. No remote run exists.
- One scenario only: a single client, the fallback to a nearly spent namespace
  is covered in source only.
- The production supervisors (`recovery`, `screenrecovery`) do not call the new
  helpers. The scenario supervisor is a fixture; wiring a production one needs
  an application-specific drain.
- Nothing warns an operator on its own: a supervisor must read the report.
- Handle, endpoint and IRQ retirement cannot be cured by replacing a client; the
  report shows them, and the only recovery is the whole-system restart.
- Rates in the operating limit are arithmetic, not measurements.

## Done when

A supervisor can read remaining lifetime, replaces a client before exhaustion
in a CPU run, and the documented lifetime is either extended by the wider ABI
or accepted with a written operating limit.
Met: readable report, replacement before exhaustion in a CPU run (client replaced with 4095 calls left, replacement served from a fresh namespace, no `EOVERFLOW`), written limit. Packaging the evidence is G6's job.

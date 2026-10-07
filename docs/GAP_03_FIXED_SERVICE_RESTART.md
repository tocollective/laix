# G3. Fixed UART and Screen services cannot be restarted automatically

[Gap checklist](GAPS_CHECKLIST.md) · [Previous](GAP_02_KERNEL_POLICY.md) · [Next](GAP_04_FINITE_LIFETIMES.md)

Date: 2026-10-07; revised 2026-10-08. Priority: **P2**.

## Criterion

A failed service should be replaceable under explicit authority. This held for
the Disk/Files runtime graph only.

## Current state

Revised 2026-10-08. Contract: [SERVICE_RECOVERY.md](SERVICE_RECOVERY.md#supervised-display-screen-and-bitmap-storage).
Acceptance: [SCREEN_RECOVERY_ACCEPTANCE.md](../tests/SCREEN_RECOVERY_ACCEPTANCE.md).

- Disk/Files recovery is supervised, with fresh identities, transactional
  publication and quiescent handover ([SERVICE_RECOVERY.md](SERVICE_RECOVERY.md)).
- **Screen is now restartable in a new profile**, `LAIX_CONSOLE=screenrecovery`.
  The supervisor launches bitmap storage and Screen, and `grantTaskDevices`
  accepts `SCREEN` and `FONT`. The kernel installs exactly the Screen role's
  device-table rows into an unpublished child after a preflight (no live owner,
  every range free in the resource ledger, engine not BUSY), so the old owner's
  directory must be gone first. Screen and its bitmap storage share a resource
  generation and are replaced together by `recoverChain` (bounded attempts,
  1/2/4/8 s backoff, explicit client reconnect through the resolver).
- The **fixed boot profiles are unchanged**: the default UART bootstrap and the
  `screen` bootstrap keep their sealed grants and have no supervisor. A dead
  fixed Screen still leaves clients with `EPIPE` until reboot. The restartable
  Screen is a different startup path (a `RecoveryStart` record), not a retrofit.
- It is a separate profile because the disk broker has one owner: bitmap storage
  and Disk/Files cannot run at once.
- **UART decision: documented as fixed.** The default console server holds only
  the narrow UART TX operation and no state, and the kernel's panic and debug
  output use the independent emergency UART, so its loss costs application text
  only. The default profile has no supervisor, and adding one would give up the
  seal-before-entry bootstrap for little gain. The runtime factory already
  accepts `DEVICE_UART_TX`, so a profile that wants a restartable console can
  supervise one with the same policy; none is built or claimed here.
- **Device reset: specified as unavailable.** WRM has no per-device reset or
  abort; the only reset is the machine-wide power controller `RESET`
  ([DEVICE_CONTRACT.md](DEVICE_CONTRACT.md#device-reset-g3-specified-as-unavailable)).
  A permanently BUSY storage device therefore stays quarantined, as before.
  Screen needs no reset: it issues no DMA.
- Screen medium swap between bitmap halves and cache-hit validation still have
  no CPU campaign.

## Evidence

- Source: [test_screen_recovery](../tests/test_screen_recovery.py) (13 cases:
  exact row install, owner/ledger/BUSY preflights, no partial state on
  failure, 8 replacements back to the free-page baseline, font owner replaced
  while Screen lives, DMA-pin refusal, policy order and exhaustion).
- CPU, on the existing `bin/wrm081632` and `bin/firmware.rom`
  ([probe](../tests/probe_screen_recovery_cpu.py)): a scenario image kills Screen
  mid-frame four times (five generations); every replacement renders a distinct
  frame; a watchdog image lets the production supervisor and client meet three
  real Screen faults; a production smoke image draws the status line.

## Remaining limits

- A Screen failure also replaces bitmap storage, because they share a generation.
- The restartable Screen exists only in `screenrecovery`. Its Screen starts with
  an empty frame, cache and cursor; the client redraws.
- A failure inside the display grant after its preflight (page-table
  exhaustion) leaves an unusable child that the supervisor must discard.
- The CPU runs are on the working tree, not an identified acceptance bundle
  ([G6](GAP_06_EVIDENCE_AND_CI.md)); `screenrecovery` is not yet one of the CI
  profiles.
- No device reset exists, so the quarantine of a permanently BUSY disk is final.

## Done when

Killing Screen repeatedly (a bounded number of generations) restores
rendering without reboot, stale Screen handles stay invalid, and resources
return to their baseline. UART policy is decided separately: restart, or
document it as fixed (the emergency UART is independent either way).
Met for the supervised profile; the fixed boot profiles remain, by decision, unrestarted.

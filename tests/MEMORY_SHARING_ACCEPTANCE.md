# Shared-region lifetime acceptance

Date: 2026-10-05. Scope: explicit whole-region grants and safe owner/borrower
teardown in both orders. The previously deferred [A2 acceptance item](../docs/AUDIT_02_RUNTIME_MEMORY.md)
is complete. [The contract](../docs/RUNTIME_MEMORY.md#explicit-sharing-and-both-death-orders)
specifies the authority, permission ceiling, mapping bitmap, independent lease
pins, revocation and bounded orphan accounting.

## Checked-source results

`test_memory_sharing.py` executes the actual checked M syscall/MMU/allocator and
task-lifecycle paths. Its seven tests passed. They establish:

- Owner first: its directory/stack are reaped, the old budget closes, its slot
  becomes generation 257, and the borrower still has its original shared frames
  and data. Final borrower death returns those exact frames without refunding
  the replacement's budget. A later allocation, with the fixture cursor directed
  at the released originals, reuses zeroed frames and rejects the stale grant.
- Borrower first: its mapping and lease pins disappear while the surviving owner
  retains its allocation, mapping and data. Owner death releases the frames.
- Permission ceilings deny borrower RW/RX upgrades from an RO grant, foreign
  grant use/close, allocation release and stale tokens. A mapped borrower cannot
  drop its lease without unmapping. Revocation allows downgrade but denies
  renewed mapping and permission upgrades.
- A partial unmap clears only the matching per-page bits. The final bit releases
  a revoked grant's lease. Two independent borrowers keep an orphan alive until
  both are gone; an independent retained pin further delays release and is
  reclaimed only after the final pin drops and the orphan sweep runs.
- Cross-task W^X rejects a writable borrower while the owner has RX and rejects
  owner RX while the borrower has RW. Grant creation itself pins ownership,
  without claiming mapping access or bypassing alias checks.
- Failure at either staged table in a two-directory mapping leaves borrower
  charges, free pages, mapping bits and original lease pins unchanged.
- Eight-grant lender/borrower limits, full task generations, retired grant rows,
  invalid self/foreign targets, W+X and overflowing lease-reference acquisition
  are denied without a partially acquired lease.

The full checked-source regression run passed **319 tests**. The final focused
sharing recheck passed seven tests, including the subsequently added limit,
retirement and lease-overflow case. Existing private-region and task-lifecycle
focused regressions also passed (eight and fourteen tests). Source/device pin
fixtures establish reference conservation; they do not constitute physical DMA
hardware acceptance or a new user DMA API.

## CPU results

The dedicated image boots an owner, borrower and observer. Two temporary
machines run the same image. The second machine changes only initial startup
argument data before `taskStart` to select borrower-first teardown; executable
instructions and disk artifacts are unchanged. The user components communicate
through bounded IPC, create/map/close grants through real syscalls, exit normally
and wait for actual selected-stack reaping.

```sh
LAIX_CONSOLE=sharing sh laix/build.sh
python3 laix/tests/probe_memory_sharing_cpu.py laix/build/sharing.img laix/build/sharing.map
```

The probe requires `build/sharing-user/sharing.map` from that build and records
UART, monitor transcripts and hashes in `build/acceptance/memory-sharing/`.
It needs a localhost monitor socket and uses the existing WRM executable/ROM;
neither WRM nor the ROM was rebuilt.

| CPU case | After first death | After final death |
| --- | --- | --- |
| Owner first | Owner slot 1 is reused as Created reference 257 with a distinct budget. The orphan still records allocation owner 1. Both original frames remain allocated with two references each (borrower mapping plus grant lease); borrower PTEs still map those exact frames RW/NX, and both data words are intact. The borrower reads them after owner reaping and slot reuse. | Both original frames become free, with owner and reference counts zero. Orphan charge and region row disappear. The replacement is later configured, published, collected and reaped normally. |
| Borrower first | Borrower root/stack are reaped. Each original frame retains one owner mapping reference, owner 1 and intact data. No orphan charge is created; the owner's region and directory remain live. | Owner reaping returns both original frames and clears their ownership/references. |

Both final user fixtures exit with code zero. There are no remaining memory
budgets, regions, grants, memory-space capabilities, orphan charges or user
frames. The physical free-frame counter equals the final page-purpose ledger.
No frame is available to the allocator between the two deaths in either case.
The private-memory CPU regression also passed against the extended MMU paths:
heap/IPC/preemption, supervisor RO aliases, W^X, exhaustion recovery and loading.

The sharing probe covers normal CPU death/reaping and slot reuse. Additional
borrowers, grant permission attacks, staged-table failures and held pins are
checked-source cases. Sharing is bounded whole-region lending, with no regrant,
subregion grants, ownership transfer, forced remote unmap or demand paging.

## Provenance

The dedicated sharing image was built from the working tree. The base commit
is `7982ff8f31756de15b6848d0dbefe222cad7eecb`; changed build inputs and test/probe sources are hashed in
`build/acceptance/memory-sharing/sources.json`.

| Artifact | SHA-256 |
| --- | --- |
| image | `04f336c0bda2849272ca31410db8bef66544d9d35ccce62c290d27cb8e8da284` |
| map | `4a64791a6ff48fef29ece6d9ed9c3bd20720bb8649062edb309c495fc8375502` |
| user_map | `fe90d0ecd616d61e6541e17052da04af08fddca00560fa3393a3b7616efc455a` |
| emulator | `5e7d9f7f90ccbc7929b894c118fecbecb04fe58165b16f1843f7412026e2448c` |
| rom | `cab72f0aa602b35f746555c05289be61cd8271bba11ee4d773c7a625230c9965` |
| Source manifest | `41a328cf01cfa5135d1ba4fccb6babd6a3d965e576f9f52791e00147a76018c4` |

Shell syntax and `git diff --check` passed. The acceptance probe hashes input
image, kernel/user maps, emulator and ROM before and after both cases.

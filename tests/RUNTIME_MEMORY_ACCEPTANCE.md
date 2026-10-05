# Runtime memory acceptance

Date: 2026-10-05. Scope: private eager regions, task budgets, scoped mapping
rights, a minimal user heap and bounded unpublished-child population.

The private eager-memory implementation in [A2](../docs/AUDIT_02_RUNTIME_MEMORY.md)
is accepted within this scope. [The runtime memory contract](../docs/RUNTIME_MEMORY.md)
defines the exact rights, caps, errors, transaction behavior and teardown order.
Explicit sharing has since been implemented and is accepted separately in
[the sharing record](MEMORY_SHARING_ACCEPTANCE.md). The approved task factory still selects the executable entry.

## Checked-source evidence

`test_runtime_memory.py` has eight tests that execute checked M ASTs and the
actual syscall dispatcher. The heap test executes `user/heap.m` and its wrappers
against that dispatcher. These tests generate no machine instructions.

| Requirement | Evidence |
| --- | --- |
| Region and address-space authority | Attenuated rights, foreign caller/region/target rejection, stale capability and region tokens; nontransferable rows use full task references. |
| Budgets and progress reserve | Partial three-frame issuance fails at each frame; complete rollback refunds charges. With 2 MiB RAM, a directory plus 95 frames reaches the 96-page task quota while physical memory remains available; a peer can still allocate. Physical reserve exhaustion is checked separately. Trusted reserved owners can use the 16-frame reserve while application allocation is denied. |
| Bounded object capacity | Four space capabilities and eight regions per caller/target leave capacity for a peer. Retired space/region generations return ENFILE without wrap. |
| Alignment, range and occupied behavior | Zero/oversized counts, unaligned addresses, out-of-region offsets, kernel/stack and wrapping/end-crossing spans are denied. A last-page collision changes no earlier leaf. |
| Multi-page transactions | A two-page span crosses a directory boundary. Failure at either staged table allocation restores free-page sets, budget charges and frame references. A hole in protect/unmap prevents any preceding edit. |
| Zeroing and reference conservation | Reissued frames contain zero throughout both pages. Release is EBUSY while mapped or independently pinned through retainPage, and succeeds only after both reference classes disappear. |
| Alias W^X | W+X and RW aliases of RX frames fail. Two RX aliases prevent RW protection until the other alias is removed. Shared supervisor identity leaves become RO. Existing `test_wx_aliases.py` also covers existing/future directories and permanently NX stack purposes. |
| Loader bounds | Cross-page population copies the exact bytes. Destination overflow, a source crossing an unmapped page and writes to executable frames fail without partial writes. Publication revokes the loader capability. |
| Teardown | Mapped and never-mapped regions are reclaimed after another task is selected; budget closes at zero; a reused child rejects the old loader token. Existing lifecycle/DMA tests retain the victim's entire allocation ledger until physical quiescence. |
| User allocator | Two heap allocations grow/shrink by complete pages; interior free is denied, table exhaustion rolls back the frame allocation, and zero/overlarge byte requests fail. |

`test_runtime_tasks.py` additionally injects failure at every construction
allocation and mapping boundary, including the directory, guarded stack and
startup frame. `test_runtime_task_acceptance.py` covers failure after successful
acquisitions, supervisor death and DMA quarantine under slot pressure. These
are source/device-fixture results, not CPU exhaustion injection at every site.

## CPU evidence

The separate `LAIX_CONSOLE=memory` image embeds the dedicated user ELF fixture.
It compiles/links LA/IX and its user components only. WRM and the ROM are existing
executables; neither was rebuilt. Reproduce with:

```sh
LAIX_CONSOLE=memory sh laix/build.sh
python3 laix/tests/probe_runtime_memory_cpu.py laix/build/memory.img laix/build/memory.map
```

The probe requires `build/memory-user/memory.map` from the same build. It hashes
all input artifacts before and after the run, uses a temporary machine/disk and
records `build/acceptance/runtime-memory/results.json`, UART and monitor logs.
Local monitor socket access is needed; it does not contact an external service.

The final CPU run passed:

- Twenty two-page heap lifetimes and twenty IPC round trips with a separate user
  peer. A breakpoint in `taskTick` proves a natural timer interrupt during work.
- All eight private-memory syscalls, including six-argument map, close and loader populate,
  runs through the generated user trap path.
- A mapping crosses a 4 MiB directory boundary. Occupied-span and wrapping
  requests are rejected. Mapped release is denied.
- RW-to-RO-to-RX transitions preserve reads, and the CPU executes the issued
  code and returns 42. During that execution the probe reads both actual user
  leaves as RX and both shared supervisor physical aliases as RO/NX; it verifies
  that the caller's directory inherits that exact shared identity table.
- Writable aliases and RW protection with another RX alias are rejected. After
  unmap/protection removes X, user writes work again.
- Allocation reaches ENOMEM through the real bounded path, releases every
  issued region and successfully allocates/reuses zeroed heap memory afterward.
- The loader populates a Created child, maps/protects its region RX, is denied
  further population, publishes the child and loses allocation authority.
- Both user fixtures exit with code zero; the child is collected. Reaping leaves
  no budget owner, region owner, memory capability, user frame, victim directory
  or victim stack. The free-frame count matches the final physical page ledger.

The CPU probe does not inject faults at each allocator site or hold physical
DMA BUSY; those adversarial results come from checked source and device fixtures.
It covers private eager memory, not arbitrary ELF loading or paging. Sharing CPU evidence is recorded separately.

## Provenance and checks

The image was rebuilt from the working tree after the memory changes. The
source base is `7982ff8f31756de15b6848d0dbefe222cad7eecb`; uncommitted source hashes are in
`build/acceptance/runtime-memory/sources.json` (manifest SHA-256 below).

| Artifact | SHA-256 |
| --- | --- |
| image | `6164e1c56fdd29f4e5ee2fb2d910f2113676d823d5121d25a84af915ebbaa8d0` |
| map | `209e9dfa700a9722ce0cfd8b489e9e48bbb96f824a9085d0ff93e095148f961c` |
| user_map | `f752ffc79d6dcc00d94b5f3fe12bd5810c1526c46b4b2aaab1f7a311e5d4b3f8` |
| emulator | `5e7d9f7f90ccbc7929b894c118fecbecb04fe58165b16f1843f7412026e2448c` |
| rom | `cab72f0aa602b35f746555c05289be61cd8271bba11ee4d773c7a625230c9965` |
| Source manifest | `41a328cf01cfa5135d1ba4fccb6babd6a3d965e576f9f52791e00147a76018c4` |

The full LA/IX checked-source regression suite passed **313 tests**, including
all eight dedicated runtime memory tests; the subsequent full regression with
sharing passed 319 tests and the final sharing recheck passed seven tests. The final focused runtime-memory
recheck additionally isolates quota exhaustion from physical exhaustion. Shell
syntax checks and `git diff --check` passed. The dedicated LA/IX memory image
compiled/linked and the CPU probe passed; WRM was not rebuilt.

The fixture introduces no WRM source/build changes. Existing ready UART,
screen, services and supervisor image bytes were left unchanged.

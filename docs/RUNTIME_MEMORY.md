# Scoped eager runtime memory

The runtime eagerly allocates owned regions and shares them only through explicit grants. Page allocation,
reference enforcement and mapping authorization stay in the kernel; block
allocation policy lives in `user/heap.m`. Demand paging and user DMA pins are deferred. Ordinary applications cannot
name physical addresses, owner IDs, page tables or directory pointers.

## Objects and authority

A `MemoryRegion` owns 1–16 distinct, zeroed 4 KiB PAGE_USER frames, possibly
noncontiguous. Its kernel ledger records the allocation owner, page count and
frame addresses. Ownership is independent of mappings. Each map retains the
frame; unmap releases that reference. The existing broker's DMA pin uses the
same reference counter and independently prevents allocation release.

A `MemorySpace` capability names a task's existing address space, an exact
caller reference and a rights mask: ALLOC/release=1, MAP=2, UNMAP=4, PROTECT=8,
POPULATE=16, SHARE=32 (ALL=63). Rights are checked for each operation. These capabilities are
nontransferable and cannot be copied through endpoint-handle operations.
Space and region tokens are positive `(generation << 8) | (slot + 1)` words. Generation starts
at one, advances before issuance and never wraps beyond `0x7FFFFF`. Closing,
publication or death invalidates the row; a token from an earlier lifetime
cannot authorize a replacement. Region tokens identify allocations; they
convey no authority without a matching space capability.

Opening target 0 selects self. A foreign target requires the caller's scoped
CONFIGURE task capability and a Created target. Self cannot acquire POPULATE.
A loader can allocate, map and fill only the target's private runtime regions,
within its budget. Foreign capabilities stop working as soon as the target
leaves Created. Successful publication also clears their rows. Caller death
revokes its capabilities; an unpublished child is discarded by existing
supervisor teardown. A published child owns its regions independently. SHARE
can issue grants only for the calling task's own regions; loader authority cannot
lend child allocations.

There are 32 space rows, at most four per caller, and 64 region rows, at most
eight per target. These bounds fit the eight live task slots before any orphan
retention. Orphan rows and generation retirement reduce available global capacity;
exhaustion returns ENFILE and never reuses a stale identity.

## Budgets and reserved progress

A task is enrolled before its first directory allocation. Its 96-frame hard
limit covers the directory, private tables, image/data/startup/stack frames,
all eager regions, and the complete guarded kernel-stack run, including its
guard and any private supervisor table. A failed constructor closes its empty
budget; physical teardown closes the budget only after its charge reaches zero.
The shared kernel mappings and their root/tables belong to the kernel budget,
not to every inheriting task. Broker bounce buffers belong to trusted broker
owners rather than being hidden application charges.

Application and user-supervisor allocation cannot reduce the free pool below
16 frames (64 KiB). Only reserved trusted kernel/idle/broker owners can use that
pool. This preserves resources for supervisor/kernel mechanisms, including
idle-stack setup and bounded DMA completion. The user supervisor retains its
already charged code, stack and mailbox; scheduling, IPC cancellation, task
completion, collection and reaping allocate no memory. The reserve is protected
from ordinary runtime allocation, not a promise that every future device
request succeeds. Bootstrap fails cleanly if installed RAM cannot support it.

Limits are allocation caps, not promises to precommit 96 frames for every task.
A task can hit its cap while a peer with remaining budget continues allocating;
global physical pressure can also return ENOMEM. Allocations never steal or
reclaim a peer's existing mappings. Free refunds the original responsible
budget. Unmapping the last leaf in a private table also refunds that table.

## Syscall ABI

Arguments occupy r1–r6; r9 selects the syscall. Positive results are tokens;
ordinary success is zero; negative results are errno values. Counts and offsets
in map are pages; populate's offsets and length are bytes. User wrappers are in
`user/syscalls.m`. No operation blocks or changes the selected task.

| Number | Operation and arguments | Required rights | Result |
| --- | --- | --- | --- |
| 42 | space(target reference or 0, rights) | self or foreign task CONFIGURE | space token |
| 43 | allocate(space, page count) | ALLOC | region token |
| 44 | release(space, region) | ALLOC | 0 |
| 45 | map(space, region, VA, region page offset, count, PTE permissions) | MAP | 0 |
| 46 | unmap(space, VA, count) | UNMAP | 0 |
| 47 | protect(space, VA, count, PTE permissions) | PROTECT | 0 |
| 48 | populate(space, region, byte offset, caller source VA, byte length) | POPULATE; Created target | 0 |
| 49 | close(space) | exact caller capability | 0 |
| 50 | grant(space, region, borrower reference, PTE permission ceiling) | SHARE; self target | grant token |
| 51 | mapGrant(space, grant, VA, PTE permissions) | MAP; exact borrower, self target | 0 |
| 52 | closeGrant(grant) | exact lender or borrower | 0 |

Mapping ranges must be nonempty, page aligned, at most 16 pages, and entirely
inside `[0x60000000, 0xA0000000)`, a subset of the validated user window.
Lengths use subtraction before addition; zero, oversized and wrapping spans
are rejected. Region offsets must fit the allocation. Protect and unmap accept
only leaves belonging to live runtime regions of that target. An exact borrower ledger additionally authorizes granted leaves. Fixed code,
startup, stack/guard, kernel and device mappings are outside this authority.
An occupied destination returns EBUSY; map never replaces an existing leaf.
Unmapping a hole fails the entire span rather than silently skipping it.

PTE permissions require V/U/R and permit only R, RW or RX. Caller A/D/G and
reserved bits are rejected. W+X is rejected. Executable access excludes every
writable user alias of that frame and makes its shared supervisor physical
window RO across every directory. Break-before-make plus FENCE/TLBI.ALL removes
stale W/X translations before access changes. Stack purposes remain NX in the
underlying MMU API; runtime calls cannot edit fixed stack mappings.

Protect prevalidates each requested leaf against all existing aliases. It
conservatively rejects an RW-to-RX or RX-to-RW transition if another conflicting
alias exists, even when that alias is in the same requested span. Remove the
conflicting aliases or protect them to RO first, then request the transition.
No operation implicitly weakens another alias's rights.

## Transactions, exhaustion and rollback

The one-CPU trap path excludes IRQs. Capability lookup asserts that condition;
MMU and allocator helpers preserve the caller's interrupt state. No scheduling
window exists between validation and commit. This is not an SMP protocol.

Allocation checks count and row limits, then issues zeroed frames. If any frame
allocation fails, every frame issued by that operation is freed, charges are
refunded and the row remains unissued. A failed issuance may consume a token
generation; it does not consume allocation ownership.

Map validates the whole span, access conflicts, destination vacancy and frame
references first. A 16-page span touches at most two 4 MiB directory slots.
`mmuMapRegion` allocates and zeros any missing tables into a temporary ledger,
without publishing parents. Failure frees staged tables and changes no existing
PTE, mapping reference, access count or budget charge. Only after every fallible
step succeeds does it retain frames, revoke supervisor W as needed, write leaves,
publish parents and invalidate translations. The existing single-page trusted
API remains available to bootstrap.

Unmap/protect validate all leaves and permissions before any edit. Their commit
phase allocates nothing and cannot ordinarily fail; an accounting invariant
violation is a kernel panic. Release requires no outstanding grants, then validates every frame's ownership,
zero mapping/DMA references and zero W/X access count. EBUSY leaves the entire
allocation intact. It never implicitly unmaps aliases or cancels DMA.

POPULATE accepts at most the region's 64 KiB and validates the complete caller
source and destination before copying any byte. Invalid source is EFAULT without
partial writes. Executable destination frames are EBUSY: the loader must remove
all X aliases before writing. Typical loading is allocate, populate, map RW if
needed, protect RX, configure, publish. Protect removes all W for executable
frames, including supervisor W; publication removes foreign loader authority.
The current approved-image task factory still chooses the entry and initial
code. This API permits bounded population of additional regions, not an
unrestricted ELF parser, arbitrary task entry point or directory constructor.

Error meanings: EPERM for missing/stale/foreign authority or an unowned mapping;
EINVAL for invalid counts/ranges/flags; ENFILE for bounded ledger exhaustion;
ENOMEM for budget, reserve, physical-frame or private-table exhaustion; EBUSY
for occupied addresses, alias conflicts or outstanding references; EFAULT for
an invalid populate source. Existing mappings remain usable after failed work.

## Teardown ordering

1. Mark Dead, remove ready/wait links, cancel IPC and device operations, revoke
   caller and target memory capabilities, and record completion.
2. Retain the victim's allocation ledger and charges while DMA is BUSY. Logical
   cancellation does not drop a hardware pin or make the slot reusable.
3. Select another address space and guarded kernel stack. Reaping checks both
   and waits for the trusted broker's physical quiescence.
4. Destroy the inactive directory: detach mappings, invalidate all translations,
   drop mapping/access references, restore supervisor permissions and free
   unreferenced owned frames, private tables and root. Borrowed frames remain
   allocation-owned by their original lender; dropping a leaf clears its exact
   grant bitmap bit and never frees a surviving owner's allocation.
5. Drop the dead borrower's remaining leases. Reap the owner's private regions;
   regions with surviving grants become detached orphan allocations. Move their
   charges to the bounded orphan ledger, retaining their original allocation
   owner and generation. Release startup/construction pages and the inactive
   kernel stack; do not reset region or grant generations.
6. Close the zero-charge budget. Only then may a runtime task slot become Empty.

## Explicit sharing and both death orders

`grantRegion` issues a nontransferable whole-region grant for a named, live task
reference, with an R, RW or RX permission ceiling. The owner communicates the
token through IPC. Only that borrower may map it, into its own authorized
runtime window, at most once at a time. A grant covers all 1–16 region frames;
there is no subregion lending, regrant, owner transfer or arbitrary frame access.
There are 64 grant rows, at most eight offers per lender and eight simultaneously
mapped grants per borrower. Unsolicited offers and their lease pins spend only
the lender's quota; borrower charging begins with explicit map acceptance.
See [the transfer/accounting contract](CAPABILITY_TRANSFER.md).
Generations advance on issuance, retire without wrap and survive task-slot reuse.

Each grant retains one lease pin per frame, independent of the actual borrower
mapping references. The `MemoryGrant` ledger holds full lender/borrower references,
the region token and permission ceiling, one virtual base, a per-page mapping
bitmap and whether new mapping/upgrades are still allowed. A failed multi-page
map rolls back staged tables, mapping bits and charges while preserving the
existing grant lease. Partial unmap clears exact page bits; all bits must clear
before the grant can be mapped again. Tables always charge the borrower budget.
The owner's region release is EBUSY even for an unmapped but open grant.

Foreign MMU leaves are valid only if the exact borrower, VA, frame and permission
ceiling match a live ledger entry. This replaces implicit same-owner-only
validation in checked region mapping, user-buffer copies, protect, unmap and
teardown. The private single-page kernel map helper remains owner-only. Every
borrower alias participates in the existing global W/X counters and shared
supervisor alias protection. A borrower cannot protect beyond its ceiling,
release the allocation, populate it as a loader or regrant it.

Closing by the lender revokes future mapping and permission upgrades. Existing
borrower leaves retain their installed access until unmap or borrower death;
revocation does not forcibly edit a running foreign address space. Closing by
an unmapped borrower releases its lease. A mapped borrower receives EBUSY and
must unmap first. A revoked grant drops its lease automatically when its last
mapping bit clears. Both lender and borrower death revoke the relevant grants;
unmapped ones close immediately, mapped ones survive through TLB invalidation.

In borrower-first teardown, its inactive directory drops all mapping pins and
then its lease. The owner continues to own its frames and mappings until it
explicitly releases them or dies. No borrower operation refunds the owner's
allocation while that owner remains alive.

In owner-first teardown, its directory and stack are reclaimed and its memory
capabilities disappear. Regions with surviving grants become orphan allocations:
`memoryBudgetDetach` removes their frame charge from the retiring task budget,
`memoryOrphanPages` records the retained physical charge, and the region keeps
its original full allocation-owner reference. The task's zero-charge budget can
close and its slot can become a different generation while borrowers keep their
mappings and data. Borrowers cannot create new mappings after owner death. TaskEvent RECLAIMED
confirms the original task's root/stack/budget retirement; outstanding shared
orphan frames have their own lifetime and can remain pinned beyond that event.

The orphan ledger is bounded by 64 region rows of at most 16 pages (1024 frames,
4 MiB), and consumes the already issued application frames; it cannot draw from
the 16-frame progress reserve. Orphan rows count against global region/grant
capacity until final release. The last borrower mapping and lease release permits
reclamation only if all other mapping references and DMA pins are also zero.
`memoryReapOrphans` retries pin-delayed reclamation on the selected kernel stack.
Free uses the old exact allocation-owner reference, so it increases physical
capacity without refunding a replacement task's budget. No original frame is
available to the allocator before that final reference disappears.

[Sharing acceptance](../tests/MEMORY_SHARING_ACCEPTANCE.md) covers both death
orders, owner-slot reuse, multiple borrowers, stale tokens, permission ceilings,
rollback and independent pins. The CPU fixture also checks the original physical
frame ownership and references between deaths, and their exact release afterward.

## User heap and evidence

`heapAllocate(bytes)` rounds 1–65536 bytes up to pages, obtains a private region
and maps it RW/NX into one of eight fixed 64 KiB heap slots. It returns a
page-aligned pointer or null with `heapError`. It is deliberately one region
per allocation; there is no sub-page packing, realloc, sbrk or thread safety.
Metadata lives in the program's data segment. Failed mapping releases the new
region. `heapRelease` accepts only allocation bases (null is harmless), unmaps
all complete pages and releases the region and any now-empty table. A denied
release attempts to restore the writable mapping; user DMA is currently absent.
Raw runtime calls must not mutate mappings managed by the heap library.

[Runtime memory acceptance](../tests/RUNTIME_MEMORY_ACCEPTANCE.md) separates
checked-source failure injection from actual CPU heap/IPC/preemption, alias
execution and loader publication evidence.

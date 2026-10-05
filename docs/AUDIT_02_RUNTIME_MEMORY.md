# A2. Runtime memory authority

[Audit overview](LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_01_TASK_LIFECYCLE.md) · [Next item](AUDIT_03_RUNTIME_CAPABILITIES.md)

Date: 2026-10-05. Priority: **P1**.

The implementation checklist is complete for eager regions and explicit sharing grants. Acceptance
claims are limited to the evidence in the [runtime memory record](../tests/RUNTIME_MEMORY_ACCEPTANCE.md).
Owner-first and borrower-first sharing teardown are accepted in
[the sharing record](../tests/MEMORY_SHARING_ACCEPTANCE.md).

## Dependencies

Design with A1 and A3. Shared memory and user paging in A10 depend on this authority model; neither is necessary for the first eager allocator.

## Current state and scope

Scoped allocation/release, map/unmap/protect and unpublished-child population
are implemented in [runtime.m](../src/mm/runtime.m), using the allocator and
alias-aware MMU. Task construction enrolls its budget before directory creation;
death revokes authority and selected-stack reaping releases mapped and unmapped
regions before closing the budget. [The contract](RUNTIME_MEMORY.md) specifies
objects, rights, limits, rollback, error behavior and teardown ordering.

Applications can grow and shrink an eager heap through [heap.m](../user/heap.m).
The bounded loader can populate additional private regions of a Created child;
the approved image factory still selects its executable entry. Arbitrary ELF loading and user paging are outside this milestone.
[Sharing grants](RUNTIME_MEMORY.md#explicit-sharing-and-both-death-orders)
separately pin allocations and account for borrowers and orphaned regions.

## Implementation approach

Eager page allocation is enough for this milestone. Keep heap allocation policy
in a user library or memory service and keep page authorization, mappings and
lifetime enforcement in the kernel. A memory manager should receive a bounded
pool or budget; it should not receive an unrestricted physical-memory syscall.

For each operation, decide who owns the allocation, who pays for page tables,
and which surviving reference prevents reclamation. IPC reply buffers must be
revalidated after a wait, as they are today; granting another task mapping
control introduces new reasons those buffers can change while the client sleeps.

## Implementation checklist

- [x] Define the smallest supported memory objects: owned frames or regions, and an address-space object with explicit mapping rights.
- [x] Define allocation budgets for applications and a reserved budget for supervisor/kernel progress.
- [x] Expose checked allocation and release through scoped authority rather than user-selected owner IDs or physical addresses.
- [x] Expose map, unmap and protect with explicit target address-space authority and validated user virtual ranges.
- [x] Specify alignment, length limits, occupied-address behavior and all-or-nothing semantics for multi-page operations.
- [x] Zero newly issued user frames and account for directories/tables as part of the responsible resource budget.
- [x] Preserve W^X across every user and supervisor alias and keep stack frames NX.
- [x] Define mapping references separately from allocation ownership; prevent release while mappings or DMA pins remain.
- [x] Implement explicit whole-region grants and borrower mapping/lease ledgers; validate foreign frames only against those grants.
- [x] Add a minimal user allocator that grows a page-backed heap and releases complete unused regions.
- [x] Give the runtime loader bounded authority to populate an unpublished task address space and then remove writable code aliases.
- [x] Document memory exhaustion, rollback and teardown ordering before enabling runtime operations.

## Acceptance checklist

- [x] Grow and shrink an application heap while another task exchanges IPC and timer preemption continues.
- [x] Deny mapping of kernel memory, foreign frames, foreign directories, device ranges and wrapping virtual spans.
- [x] Reject W+X and writable aliases of executable frames; protect/unmap must invalidate translations correctly.
- [x] Inject exhaustion at frame, table and directory allocation points and verify that existing mappings remain usable.
- [x] Check zeroed memory on reuse, reference conservation and denied free of mapped/pinned frames.
- [x] Destroy the original owner and a borrower in both orders without premature frame reuse; [source and CPU evidence](../tests/MEMORY_SHARING_ACCEPTANCE.md).
- [x] Exercise each new memory API on CPU; source evaluator results alone do not establish the generated mapping path.

## Completion record

[Runtime memory acceptance](../tests/RUNTIME_MEMORY_ACCEPTANCE.md) maps each
requirement to checked-source and CPU evidence, including provenance and the
remaining private-region/approved-entry limits. Failure injection and DMA-pin
conservation use source/device fixtures; the dedicated memory CPU image covers
every new syscall, heap growth/shrink with IPC and a real timer interrupt,
executable-alias conflicts, loader publication and final accounting.

The sharing completion record adds exact generation-bearing lender/borrower
identity, bounded orphan accounting and successful CPU teardown in both orders,
including reuse of the dead owner's task slot while its borrower remains alive.

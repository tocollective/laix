# A2. Runtime memory authority

[Audit overview](../../docs/LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_01_TASK_LIFECYCLE.md) · [Next item](AUDIT_03_RUNTIME_CAPABILITIES.md)

Date: 2026-10-04. Priority: **P1**.

These checklists describe proposed work, not completed implementation.

## Dependencies

Design with A1 and A3. Shared memory and user paging in A10 depend on this authority model; neither is necessary for the first eager allocator.

## Current state and gap

**Evidence:** [allocPage](../src/mm/memory.m),
[mapPage/unmapPage/setPagePermissions](../src/mm/mmu.m) and address-space
construction are trusted kernel APIs. The user syscall set has no allocation,
mapping or permission operation. Ordinary boot supplies fixed code, scratch,
startup and stack mappings. The service ELF constructor adds at most three
segments and 64 image pages, but does not expose runtime growth.
`mapPage` requires the frame and address space to have the same owner.

**Consequence:** applications must fit their preallocated writable memory.
There is no page-backed heap growth, authorized cross-task sharing or resource
API from which a user memory manager/loader could build new processes.

**Needed:** define frame/memory-region and address-space authority, with scoped
allocate/map/unmap/protect operations, budgets and transactional failure
behavior. Decide which allocation policy belongs in user space. Preserve the
current zeroing, alias-aware W^X, reference checks and DMA pins. Any shared-frame
design must replace the same-owner assumption with explicit mapping authority
and account for all borrowers during teardown.

**Acceptance:** an authorized application grows and releases memory; foreign
frames/directories, wrapping ranges and W+X aliases are denied; exhaustion
affects only its budget; failed operations preserve mappings and accounting.
A user pager and demand paging are optional. Eager allocation is sufficient
for the first complete runtime.

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

- [ ] Define the smallest supported memory objects: owned frames or regions, and an address-space object with explicit mapping rights.
- [ ] Define allocation budgets for applications and a reserved budget for supervisor/kernel progress.
- [ ] Expose checked allocation and release through scoped authority rather than user-selected owner IDs or physical addresses.
- [ ] Expose map, unmap and protect with explicit target address-space authority and validated user virtual ranges.
- [ ] Specify alignment, length limits, occupied-address behavior and all-or-nothing semantics for multi-page operations.
- [ ] Zero newly issued user frames and account for directories/tables as part of the responsible resource budget.
- [ ] Preserve W^X across every user and supervisor alias and keep stack frames NX.
- [ ] Define mapping references separately from allocation ownership; prevent release while mappings or DMA pins remain.
- [ ] If sharing is included, replace the same-owner-only mapping assumption with an explicit grant and borrower-reference ledger.
- [ ] Add a minimal user allocator that grows a page-backed heap and releases complete unused regions.
- [ ] Give the runtime loader bounded authority to populate an unpublished task address space and then remove writable code aliases.
- [ ] Document memory exhaustion, rollback and teardown ordering before enabling runtime operations.

## Acceptance checklist

- [ ] Grow and shrink an application heap while another task exchanges IPC and timer preemption continues.
- [ ] Deny mapping of kernel memory, foreign frames, foreign directories, device ranges and wrapping virtual spans.
- [ ] Reject W+X and writable aliases of executable frames; protect/unmap must invalidate translations correctly.
- [ ] Inject exhaustion at frame, table and directory allocation points and verify that existing mappings remain usable.
- [ ] Check zeroed memory on reuse, reference conservation and denied free of mapped/pinned frames.
- [ ] If sharing is implemented, destroy the original owner and a borrower in both orders without premature frame reuse.
- [ ] Exercise each new memory API on CPU; source evaluator results alone do not establish the generated mapping path.

## Completion record

When work is accepted, link each completed requirement to its tests and record
the source and image provenance, remaining limitations and CPU acceptance
results. A checked box must not imply a stronger evidence level than the linked
record establishes.

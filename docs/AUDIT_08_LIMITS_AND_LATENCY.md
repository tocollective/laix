# A8. Limits and execution latency

[Audit overview](../../docs/LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_07_DEVICE_BOUNDARY.md) · [Next item](AUDIT_09_ACCEPTANCE_AND_CI.md)

Date: 2026-10-04. Priority: **P2**.

These checklists describe proposed work, not completed implementation.

## Dependencies

Applies now; revisit budgets and worst-case sections when A1–A4 add runtime work. Threads, priorities and ASID optimization in A10 are optional follow-ups.

## Current state and gap

**Evidence:** eight tasks, sixteen endpoints, sixteen handles per task,
eight waits per queue and a 32-byte IPC limit are explicit bounds. A call's
23-bit generation never resets; after 8,388,607 admitted calls from one task,
[ipcCall](../src/ipc/ipc.m) returns EOVERFLOW for all further calls from
that task. Handle/IRQ/object generations also retire safely at exhaustion.
All kernel work uses single-CPU IRQ exclusion; [trap entry](../src/trap/trap.asm)
is explicitly not nesting-safe. [MMU switching](../src/mm/mmu.m) flushes
the entire TLB on every activation.

**Consequence:** the fixed bounds are easy to reason about, but a long-lived
client has a finite RPC lifetime. IRQ latency includes syscall validation,
copying and resource reaping; no measured maximum or service budget is recorded.
Round-robin is sufficient for basic fairness but provides no priority, CPU
budget or priority-donation contract. Full TLB flushing is safe but can limit
scaling.

**Needed:** publish supported resource/lifetime limits and an exhaustion
policy. Choose a wider/nonrepeating reply identity or safe supervised client
replacement; never reset counters while stale tokens can survive. Measure
worst-case kernel sections under the supported image and memory sizes before
introducing larger buffers, runtime allocation or bulk teardown. Keep work
bounded or split it into resumable stages. Optimize ASID/TLB caching only with
an explicit lease/reuse protocol. Priorities and donation become requirements
only if differentiated latency or real-time scheduling is a target.

**Acceptance:** boundary/exhaustion tests return documented errors without
wraparound; stress keeps timer/device progress within a stated bound; an
unresponsive client cannot monopolize a shared server beyond defined policy.

## Implementation approach

The fixed arrays and message size are useful design bounds, not defects by
themselves. The missing contract is whether those limits support the intended
number and lifetime of applications, and what recovery occurs at exhaustion.
Do not enlarge all pools before defining budgets and measuring costs.

Full TLB invalidation and IRQ exclusion provide simple correctness today.
Optimization should preserve that baseline until evidence justifies a more
complex lease, locking or preemption design. A general-purpose single-CPU
microkernel does not automatically need a real-time scheduler.

## Implementation checklist

- [ ] Publish concurrent, lifetime and per-domain limits separately, including generation retirement behavior.
- [ ] Define a sustainable reply-token policy: wider identity, safe retirement/replacement or another nonrepeating scheme.
- [ ] Keep old token values from regaining authority when changing counters or recycling task slots.
- [ ] Instrument or measure maximum IRQ-disabled kernel work for copying, validation, queue operations, allocation and reaping.
- [ ] State the supported image/memory sizes and workload used for latency measurements.
- [ ] Bound work admitted by every syscall; examine capacity validation beyond the actual transferred bytes.
- [ ] Split large future allocation/teardown work into safe bounded stages if measurements require it.
- [ ] Define CPU and shared-server admission budgets where untrusted workloads can consume service capacity.
- [ ] Keep round-robin as the documented baseline; add priority/donation only with a stated latency requirement.
- [ ] Measure context-switch/IPC cost before replacing full TLB flushing with cached ASIDs.
- [ ] If ASID caching is added, specify lease reuse, invalidation and stale-translation prevention before optimizing.
- [ ] Preserve the nonnested trap assumption or implement a complete nesting-safe entry/context protocol before enabling kernel preemption.

## Acceptance checklist

- [ ] Test last-valid and first-invalid generation values and verify documented error/retirement with no wrap.
- [ ] Demonstrate the chosen long-lived client policy without reusing stale reply rights.
- [ ] Measure timer/device progress under the largest supported syscall and cleanup workloads.
- [ ] Stress shared servers with multiple callers and verify bounded admission/fairness under the stated policy.
- [ ] If work is split, inject interruption/cancellation between stages without exposing partial mappings or reclaimed resources.
- [ ] If ASID caching changes, recycle leases and run stale-translation CPU probes across mapping changes.
- [ ] Record latency measurements separately from logical source-test results; passing mocked timers does not measure timing.

## Completion record

When work is accepted, link each completed requirement to its tests and record
the source and image provenance, remaining limitations and CPU acceptance
results. A checked box must not imply a stronger evidence level than the linked
record establishes.

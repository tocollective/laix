# A7. Device mechanisms still contain service policy

[Audit overview](LAIX_MICROKERNEL_AUDIT.md) · [Previous item](AUDIT_06_CAPABILITY_TRANSFER_QUOTAS.md) · [Next item](AUDIT_08_LIMITS_AND_LATENCY.md)

Date: 2026-10-04. Priority: **P2**.

Implemented on 2026-10-05. Checked items refer to the scoped evidence in
[device boundary acceptance](../tests/DEVICE_BOUNDARY_ACCEPTANCE.md), not to
unimplemented full-disk/network/filesystem support. The published interface is
[the generic device contract](DEVICE_CONTRACT.md).

## Dependencies

A3 supplies scoped device/IRQ/operation authority; A1/A5 define owner death and handover. Bulk data paths may later use A2/A10 memory grants.

## Original audit state and gap (2026-10-04)

**Evidence:** [service_devices.m](../src/drivers/service_devices.m) parses
the LAF font header, chooses the appended bitmap extent, admits only tiny
font/disk reads and configures a fixed screen mode/palette. The nominal Disk
service reuses this font broker. [input_device.m](../src/drivers/input_device.m)
owns keyboard FIFO draining and a 32-event software queue; the user Input
server largely wraps that operation. [The dispatcher](../src/trap/trap.m)
has dedicated font/screen/input/disk syscall families.

**Consequence:** changing font storage, input policy or display configuration
can require kernel changes. The user services are isolated, but some device
and application policy remains in the kernel's trusted computing base.
There is no general block-device service: reads expose only the fixed bitmap
extent, at most 16 bytes per request; arbitrary sectors, writes and flush are
outside the current contract.

**Needed:** separate resource authorization and safe device execution from
font/filesystem/display policy. Introduce checked region/extent and DMA
operation objects with explicit ownership, pinning and completion. Allow a
trusted resource manager to grant approved ranges; keep untrusted driver
protocols in user space. Decide which safe device registers may be mapped and
which commands require a broker. Remove unused supervisor regression modules
from production linkage where practical; [build.sh](../build.sh) at the audit date
linked the legacy screen/font modules into every profile.

This is not a recommendation to grant unrestricted DMA MMIO to untrusted
drivers. WRM physical DMA bypasses the MMU, so the narrow broker is a valid
security boundary. Kernel timer/PIC handling and emergency UART may remain.

**Acceptance:** services change an allowed extent or display policy without
changing kernel logic; foreign resources and unsafe DMA commands are denied;
death, timeout and late completion preserve pins and quarantine. Full disks,
networking and persistent filesystems remain separate OS work.

## Implementation approach

A safe generic broker can remain in the kernel. The target is to make its
checks about approved resources and physical-device safety rather than a
specific font format or fixed application. Without an IOMMU or equivalent
hardware constraint, granting unrestricted DMA registers makes that driver
trusted with physical memory.

Do not confuse moving the request parser into a user service with moving the
entire driver policy. Conversely, moving unsafe commands out of the kernel
without confinement would enlarge authority rather than improve isolation.
Write a register/operation authority table before deciding each mapping.

## Implementation checklist

- [x] Inventory every device register and command, distinguishing safe read-only/status access from commands capable of physical DMA or global reconfiguration.
- [x] Document the trusted computing boundary: kernel broker responsibilities, trusted manager policy and untrusted user-driver responsibilities.
- [x] Replace font-derived kernel storage policy with an approved extent/resource descriptor issued under scoped authority.
- [x] Keep filesystem paths, font formats and service-specific request interpretation outside generic kernel device mechanisms.
- [x] Define bounded device operations with validated ranges, lengths, command bits and immutable owner/instance identity.
- [x] Keep physical bounce-buffer selection, allocator pinning, fences and quiescence enforcement trusted.
- [x] Generalize IRQ grants and completion while preserving exclusive ownership, shared-line servicing and mask/rearm behavior.
- [x] Decide which keyboard buffering and display configuration policies move into user space and which broker limits remain enforced.
- [x] Define device cancellation as logical cancellation plus physical completion/quiescence, with no promise of unsupported hardware abort.
- [x] Define approved device regrant and reject new submissions from dead or revoked owners.
- [x] Exclude legacy supervisor rendering/font regression modules from ordinary image linkage where dependencies permit.
- [x] Publish a generic device contract before adding full-disk writes, flush, networking or additional DMA engines.

## Acceptance checklist

- [x] Change an authorized storage extent or display policy in user-space configuration without modifying kernel policy code.
- [x] Deny foreign device/IRQ authority, physical-address injection, overflow, range crossing and unsafe command flags.
- [x] Check exactly-once completion and reference conservation for success, timeout, owner death and late DMA.
- [x] Use CPU/device canaries to prove that cancelled/quarantined buffers are not reused before quiescence.
- [x] Exercise shared-line causes and held levels without interrupt storms or lost wakeups.
- [x] Ensure the normal image excludes the selected regression-only modules while emergency UART remains independent.
- [x] Keep unrelated services operational during media failure, owner death and a permanently stuck device.

## Completion record

All implementation and acceptance requirements are linked individually in
[DEVICE_BOUNDARY_ACCEPTANCE.md](../tests/DEVICE_BOUNDARY_ACCEPTANCE.md).
[DEVICE_BOUNDARY_PROVENANCE.json](../tests/DEVICE_BOUNDARY_PROVENANCE.json)
records input sources, source execution results, CPU reports and image hashes.
The record distinguishes checked-source fixtures from actual CPU/device runs,
including synthetic permanent-BUSY/shared-cause cases, read-only scope, boot-only
Screen issuance, ABI migration and the current 2 MiB screen acceptance budget.

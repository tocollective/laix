# G2. Some device and loader policy still lives in the kernel

[Gap checklist](GAPS_CHECKLIST.md) · [Previous](GAP_01_STATIC_PROFILE.md) · [Next](GAP_03_FIXED_SERVICE_RESTART.md)

Date: 2026-10-07. Priority: **P2**.

## Criterion

A microkernel keeps mechanisms and moves policy to user space. A7 narrowed
the device boundary, but several restrictions are fixed in kernel code.

## Current state

Revised 2026-10-08. See [DEVICE_CONTRACT.md](DEVICE_CONTRACT.md) and
[SCREEN_IRQ_DMA.md](SCREEN_IRQ_DMA.md).

- The trusted storage broker ([service_devices.m](../src/drivers/service_devices.m))
  reads, writes and flushes up to eight sectors per operation through a kernel
  bounce page. It is read-only unless the storage root, the manager's extent and
  the drive all allow writing ([contract](DEVICE_CONTRACT.md#multi-sector-and-writeflush-contract),
  implemented for G7). IDENTIFY and scatter/gather are rejected.
- Device resources are rows in [device_table.m](../src/drivers/device_table.m):
  the MMU grant check, IRQ issuance, the disk IRQ lookup, the Screen register
  policy and the VRAM bound all read them. The previous Screen-specific constants
  are gone from `mmu.m`, `irq.m`, `service_devices.m` and the Screen bootstrap.
  Kernel hardware classes (which pages are read-safe, which pages and lines the
  kernel keeps) and the display frame check remain code.
- Approved child images are rows (`ImageRow`) loaded by `taskCatalogLoad`; IDs
  2–16 are positions in a build-issued table, and the kernel names no image.
  Image 1 is still the kernel's self-test fixture. The rows are fixed at build.
  Nothing supplies the catalog rows at runtime; children can instead be loaded
  from storage with [`SYS_TASK_LOAD`](FILES_LOADER.md), which needs no row.
- The approved storage root is a 16-byte producer-neutral record
  ([storage_root.py](../tools/storage_root.py)). The font packer is one producer
  of it, not the format.
- Input exposes a raw bounded FIFO; delivery policy is already in user space.

## Fix directions

- ~~Express device resources as table-driven descriptors~~ (done, static table).
- Define a multi-sector operation and a write/flush contract before any
  writable filesystem ([G7](GAP_07_APPLICATION_LAYER.md)); keep one pinned
  bounce owner per operation. The contract is written; implement it with G7.
- ~~Move the image catalog to data~~ (done as build-issued rows). Runtime supply
  by a user-mode manager needs a verified loader and belongs with the optional
  Files-backed loader in [G1](GAP_01_STATIC_PROFILE.md).
- ~~Give the storage root a producer-neutral format~~ (done).
- Keep timer, PIC and emergency UART in the kernel (kept; the table cannot name them).

## Remaining limits

- The table is compiled in. Changing a row means a rebuild, not a code change.
- A new read-safe register page or hardware class is a kernel edit
  (`DEVICE_READ_SAFE_PAGES`), because it is a hardware property.
- The `SCREEN_*_VA` user ABI constants must agree with the rows.
- The write path exists for G7 (`dirty`/`-EROFS` rules apply); see the device contract.
- Evidence is the 595-test source suite and the CPU profiles of the G7 campaign
  ([G6](GAP_06_EVIDENCE_AND_CI.md)), an identified bundle; no remote run.

## Done when

Adding or changing a device resource, storage extent or approved image needs
no kernel code change, and the unchanged DMA safety tests (death, timeout,
late completion, quiescence) still pass.

# Files-backed program loading (G1)

[G1](GAP_01_STATIC_PROFILE.md) · [Runtime tasks](RUNTIME_TASKS.md) · [Target workload](TARGET_WORKLOAD.md)

Date: 2026-10-08. Status: **implemented; source tests pass, CPU run passed
locally, outside any identified bundle.**

A supervisor can create a child from an ELF image that it read from the Files
service, instead of choosing one of the images built into the kernel. The kernel
adds one mechanism (`SYS_TASK_LOAD`, 76). Reading the bytes, deciding what to load
and which rights the child gets stay in user mode.

```sh
LAIX_SESSION=loader sh laix/build.sh
python3 laix/tests/probe_loader_cpu.py laix/build/loader.img laix/build/loader.map
```

## What it replaces, and what it does not

The catalog of images built into the kernel is no longer needed for **children
created at run time**. It is still needed to start the system: Input, Disk, Files
and the loader itself must exist before anything can be read from storage, so
they come from the boot catalog as before. "Replace the catalog" therefore means
that a running supervisor no longer needs a build-time row for its children.

Files serves one immutable file, so the storage volume holds exactly one image
and the loader loads that file. The volume is the approved storage root
([storage_root.py](../tools/storage_root.py)), produced by
[append_volume.py](../tools/append_volume.py) in place of the font bitmaps.
Choosing between several programs needs names, that is a filesystem
([G7](GAP_07_APPLICATION_LAYER.md)). The loader and the kernel interface do not
change when Files serves more files.

## Contract

`loadTask(image, bytes)` (syscall 76, `r1` = user address, `r2` = length):

| | |
|---|---|
| Authority | `IMAGE_LOAD_AUTHORITY` (`0x10000`) in the caller's `createImages`. It is one bit above the catalog IDs 1–16, nontransferable, and granted only by boot policy. A catalog bit does not imply it, and it does not imply a catalog bit |
| Input | A complete ELF image of 52 to 65,536 bytes in the caller's readable memory |
| Result | A positive task reference, as for `SYS_TASK_CREATE`. The child is Created with the same six control rights (63), the same quota and the same completion row; configure, publish, inspect, terminate and collect are unchanged |
| `-EPERM` | No load authority |
| `-EINVAL` | Length out of range, or the image fails the checks below |
| `-EFAULT` | Any byte of the buffer is unreadable. Nothing changed |
| `-ENFILE` | No frames for the snapshot (it needs the image's pages plus the 16-frame reserve), quota (four uncollected children), completion rows, task slots or frames exhausted. Everything the call started is rolled back |

Order of work: authority, length, **a private snapshot in contiguous free
frames**, copy, image checks on the snapshot, reserve the completion row,
construct, release the snapshot. The kernel never reads the caller's memory after
the copy, so the caller cannot change bytes between the checks and their use.
The frames belong to a reserved kernel identity (`LOAD_STAGE_OWNER`) for this one
call; the 16-frame progress reserve is never touched, and no static kernel
memory is spent (the kernel's static budget test would reject a 64 KiB buffer).
The call runs with IRQs excluded on one CPU, so it cannot be interleaved.

## Image checks

The same checks as for a catalog image, now one function
(`taskProgramValid`, [program.m](../src/task/program.m)) that both paths call
before any allocation: little-endian 32-bit WRM executable, one to three
`PT_LOAD` segments, page alignment, flags R, RX or RW only (no W+X), every
segment inside the service image window, no overlap, no more than 64 pages
in total, file bytes inside the image and not larger than memory bytes, and an
entry point that is word aligned inside an executable segment's file bytes. No
relocations, TLS or dynamic linking. A catalog image must additionally lie in the
kernel's `.rodata`.

A loaded child gets no creation authority, no device rights and no endpoint
unless its supervisor configures one. It is an ordinary user task: untrusted
code limited by the same 96-frame budget, W^X mappings and rights as any other.
There is no signature or hash: the trust decision is the load authority and the
approved storage root, which is the same trust the catalog places in the build.

## User side

- [loadfile.m](../user/loadfile.m): `readImage` reads a Files file into a buffer
  in the largest pieces Files returns (16 bytes); `loadFileImage` adds the load.
- [loader.m](../user/services/loader.m) is the acceptance loader. Its buffer is a
  64 KiB heap allocation. Every check exits with its own code.
- [hello.m](../tests/programs/loader/hello.m) is the child. No kernel row names
  it: it exists only as bytes on the volume. All three segments matter to the
  exit code `(5 + argument) * 9`, and argument `0xDEAD` takes the fault path.
- The loader boot profile is Input, Disk, Files and the loader. The kernel's
  service-start check still gives every Files client an Input endpoint, so Input
  stays even though the loader never reads it.

## Evidence

Source ([test_loader.py](../tests/test_loader.py), 10 tests): authority and
catalog independence, length and buffer errors without effect, 22 malformed
images refused without effect, snapshot semantics and its frames being released, the reserve being kept, the shared quota, and a
forced allocation failure at every allocation step with the frames and rows
conserved.

CPU ([probe_loader_cpu.py](../tests/probe_loader_cpu.py), profile `loader`, local
run 2026-10-08, 2 MiB, 128 MHz, existing emulator and ROM):

- The whole 38,788-byte image (padded to 39,424) crossed Disk and Files in
  16-byte reads, twice, and 11 children ran from it: four exits, one fault,
  four filling the quota, and two more after collection and after the failures.
  Their exit codes, flags and the fault cause match; the loader exited 0.
- The fifth uncollected load was refused with `ENFILE`; oversized, short,
  unmapped, wrong-magic and bad-segment requests were refused and the next load
  worked.
- Three tampered volumes (magic, entry point, segment address; edited in the
  probe's private disk copy) were each refused by the kernel with no child created.
- After the run no loaded child kept a slot, directory or kernel stack, and no
  task capability row remained.
- Each of the 17 `SYS_TASK_LOAD` calls returned within **1,859,714 cycles
  (14.5 ms)**, the longest being a full load (snapshot, copy, checks,
  construction); the budget for an IRQ-excluded section is 2,560,000 cycles (20 ms, G5; it was 500 ms). The
  shortest, an early refusal, took 710.

Regression on the changed kernel (2026-10-08, local): the whole source suite
passed (449 tests), and the supervisor, soak, objects, recovery (fixture and
production), latency (32 and 128 MiB), memory, sharing and services profiles
passed from locally built bundles. The shared creation path in `control.m` and the
image checks in `program.m` were refactored for this change; catalog images go
through the same code as before the change.

## Limits

- One file, therefore one program per volume. No names, no directory.
- Reading a 39 KiB image through Files takes seconds of machine time: about
  4 s by the measured [64 KiB Files read](TARGET_WORKLOAD.md#bulk-transfer-cost)
  (7.1 s). That is arithmetic from that measurement, not a separate timing of
  this loader. It is a one-time cost per load.
- A loaded image is at most 64 KiB as a file, 64 pages in memory, three
  segments: the catalog's limits, with the file size now also bounded.
- Loading needs the image's pages (up to 16) plus the 16-frame reserve free at the time of the call; with less it fails with `ENFILE`.
- Not in the checked-in provenance record, and not run on remote CI. The
  profile is added to the acceptance workflow.
- No writable storage, so nothing can be installed at run time yet; the volume
  is produced at build time ([G7](GAP_07_APPLICATION_LAYER.md)).

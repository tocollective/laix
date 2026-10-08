# Writable block path and filesystem service (G7)

[G7](GAP_07_APPLICATION_LAYER.md) · [Device contract](DEVICE_CONTRACT.md#multi-sector-and-writeflush-contract) · [Service recovery](SERVICE_RECOVERY.md) · [Files-backed loading](FILES_LOADER.md)

Date: 2026-10-08. Status: **implemented; source accepted; CPU accepted in the G7 packaging campaign (2026-10-08, 27 medium snapshots at WRITE/FLUSH, all committed states). The `fs` profile is in the CI matrix and the checked-in provenance; no remote CI run.**

```sh
LAIX_CONSOLE=fs sh laix/build.sh          # builds the image
python3 laix/tests/probe_fs_cpu.py laix/build/fs.img laix/build/fs.map
```

Everything above the microkernel is user mode. The kernel adds one mechanism:
the broker's WRITE and FLUSH ([contract](DEVICE_CONTRACT.md#multi-sector-and-writeflush-contract)).
Names, allocation, caching, commit order and recovery are policy in two user
services.

```text
client ──IPC──▶ Fs (WFS1) ──IPC──▶ Disk (sector stage) ──syscalls──▶ kernel broker ──DMA──▶ disk
```

## Authority: who may write

A write reaches the medium only if three independent parties agree. Any one
missing returns `-EROFS` (30) with no hardware access:

1. the **storage root** (`flags` bit 0, built by `storage_root.py --writable`);
2. the **extent**: the manager selected it as writable (`SYS_DEVICE_EXTENT` flags
   bit 0), or boot policy did (`diskDevicesInitWritable`, used by the `fs` profile);
3. the **drive** does not report `READONLY`.

Every other profile stays read-only. A replacement Disk owner after a death never
inherits write authority: the extent comes back read-only and
`SYS_DEVICE_FLAGS` reports *dirty at regrant* if the previous owner died with
unflushed writes. The manager's explicit `SYS_DEVICE_EXTENT` call is the decision
point, and it clears that flag.

## Disk service: the sector stage

The Disk service ([disk.m](../user/services/disk.m)) keeps the old 16-byte read
protocol and adds a 512-byte stage, because an IPC message carries 32 bytes. Only
the filesystem uses it.

| Request (16 bytes unless noted) | Effect |
| --- | --- |
| `LOAD` sector-offset | one device READ of the sector into the stage (a short last sector is zero padded) |
| `PEEK` stage-offset | 16 bytes of the stage; no device access |
| `POKE` stage-offset + 16 data bytes (32 bytes) | 16 bytes into the stage; no device access |
| `STORE` sector-offset | one device WRITE of the whole stage |
| `SYNC` | one device FLUSH |
| `FLAGS` | the extent flags |

Offsets are relative to the extent. Malformed, stale-generation or out-of-range
requests never reach the device. A device error or timeout ends the service like
a failed read (its supervisor replaces it); `-EROFS` and `-EBUSY` do not.

## Volume format (WFS1)

[tools/wfs.py](../tools/wfs.py) is the reference for the format; the service and
the tool are checked against each other.

```text
sectors 0-1  metadata copy A      sectors 2-3  metadata copy B      sectors 4-  data
```

A copy is 1,024 bytes of little-endian words: magic `WFS1`, generation, next
file serial, volume sectors, first data sector (4), CRC-32 of the copy with the
CRC word zeroed, and 31 entries of `name[16]`, size, first sector, capacity in
sectors, serial. A file is one contiguous extent of whole sectors whose capacity
is fixed when it is created. Names are 1 to 16 bytes of 0x21..0x7E, no
directories, no permissions.

A copy is trusted only after its checksum and every invariant the allocator
relies on pass: matching volume size, names valid and unique, extents inside the
data area and not overlapping, sizes within capacity, serials below the next
serial. Of the valid copies the highest generation is current. With none valid
the volume is not mounted and every request answers `-ENODEV` (19).

## Client protocol

Requests and responses are at most 32 bytes
([defs.m](../src/arch/wrm081632/defs.m) `FS_*`; client library
[fsclient.m](../user/services/fsclient.m)). A file is addressed by an **id**
(`serial << 5 | slot`) that the open call returns; the id goes stale when the file
is deleted or replaced and then answers `-ENOENT`.

| Request | Meaning |
| --- | --- |
| `OPEN name flags capacity` | `FS_CREATE` creates (capacity in sectors, 1 or more), `FS_EXCL` with it fails with `-EEXIST` if present, `FS_TRUNC` **replaces** an existing file with an empty one of the given capacity (0 keeps the old one). Returns id, size, capacity bytes |
| `STAT id` | size and capacity |
| `READ id offset count` | up to 16 bytes, stopping at the end of the file and of the sector |
| `WRITE id offset data` | 1 to 16 bytes at an offset not beyond the end of the file (no holes), stopping at the end of the sector and of the capacity; the reply says how many bytes were taken |
| `SYNC` | make everything done so far durable |
| `DELETE name` | remove a file |
| `LIST index` | the index-th file in slot order |
| `INFO` | data sectors, free sectors, committed generation, flags (writable, metadata pending, data pending) |

A read-only mount answers every change with `-EROFS`. A write at the capacity is
`-ENOSPC`; no space or no free entry is `-ENOSPC`.

## Crash-consistency rules

The service holds a working table and the table of the last commit, and one data
sector in a write-back buffer.

1. **Commit order.** `SYNC` stores the buffered data sector, flushes the device,
   then writes the metadata copy that is *not* current with generation + 1 and
   flushes again. The sectors a copy refers to are therefore on the medium
   before the copy is.
2. **Atomic metadata.** A sector is never stored partially, so a torn commit
   leaves the other copy intact; the checksum rejects a copy that is half old
   and half new. At every instant the medium holds a committed state.
3. **No early reuse.** An extent that the last commit still refers to is not
   allocated to anything else until the next commit, even if its file was
   deleted or replaced in the meantime. A crash before that commit finds the
   old files whole.
4. **Replace is atomic.** `FS_TRUNC` takes a fresh extent and a fresh id; the old
   contents stay until the commit that swaps them. This is the way to rewrite a
   file safely.
5. **Appends within capacity are invisible until committed.** The data lands
   beyond the committed size.
6. **Overwriting committed bytes in place is not atomic.** A crash can leave a
   sector with its old or its new contents, each sector whole, and no promise
   about which sectors of a multi-sector overwrite made it. Use rule 4 when the
   file must not be seen half written.
7. **Nothing is durable before `SYNC` returns zero.** What was done after the last
   `SYNC` is lost at a crash. Neither the service nor the kernel flushes on its
   own initiative.

The kernel tracks `dirty` for the extent: set by WRITE, cleared only by a FLUSH
that finishes without error; a device error leaves it set.

## Failure scenarios and recovery

| Failure | Behavior |
| --- | --- |
| Power loss at any instant | The medium holds a committed state; the next mount adopts it. Tested at every device event of a nine-step scenario, with none, all and three pseudo-random subsets of the unflushed sectors surviving |
| Fs dies during an operation | Unsynced changes are lost; the replacement mounts the current commit. Clients hold stale handles and ids and must resolve again ([SERVICE_RECOVERY](SERVICE_RECOVERY.md)). The `recovery` profile does not supervise Fs yet |
| Disk dies with a WRITE in flight | The kernel keeps the bounce page pinned until the device is idle; the write still lands. A replacement Disk owner is read-only and reports dirty-at-regrant, so Fs mounts read-only until the manager selects a writable extent again |
| Device error during a commit | The call fails, the service ends (`-EIO`), the previous commit stays current; a retried commit writes everything still pending |
| A client dies | The service holds no per-client state; its unsynced writes stay in the working table and are committed by the next `SYNC` from anyone |
| Corrupt copy | Ignored; the previous generation is used, and the next commit rewrites the damaged slot |
| Read-only root, extent or drive | Mounts read-only; changes answer `-EROFS`; the medium is not written |

## Limits

- 31 files, 16-byte flat names, extents fixed at creation, no growth beyond
  capacity, no rename (delete and create), no directories, no timestamps.
- Reads and writes are 16 bytes per IPC. A sector costs about two IPC calls per 16
  bytes written (the data) plus 33 to move a sector through the stage; the 64 KiB
  Files figure of [TARGET_WORKLOAD](TARGET_WORKLOAD.md#bulk-transfer-cost) applies
  (about 7 seconds). A shared bulk path ([OPTIONAL_EXTENSIONS](OPTIONAL_EXTENSIONS.md))
  is not built.
- One client at a time is served; the stage and the sector buffer are single.
- Volume at most 2 GiB (the storage root bound). The free-space scan and the
  allocator are linear in the 62 extents of the two tables.
- No locking between clients: two clients writing the same file interleave.
- Durability is the device's: `FLUSH` asks the host to put the image on its
  medium; the kernel can say no more.

## Evidence

Source (all against the checked M sources through the source evaluator):

- [test_block_write](../tests/test_block_write.py) (13): the three-way authority,
  alignment and length limits, a faulting buffer issuing no command and leaving
  the page free, dirty set by WRITE and cleared only by a good FLUSH, instance
  exhaustion, an orphaned WRITE keeping its page pinned and still landing, the
  regrant returning read-only with dirty-at-regrant, root flag validation.
- [test_disk_stage](../tests/test_disk_stage.py) (7): the stage protocol against a
  fake broker; malformed and stale requests never reach the device.
- [test_fs](../tests/test_fs.py) (20): mounting a reference image, reads, creation,
  capacity, in-place and unaligned writes, atomic replace, stale ids, deletion
  and space accounting against an independent recount, read-only mounts, damaged
  and forged metadata (twelve forgeries with valid checksums), generation
  choice, device failure, and the **crash campaign**: power lost at every device
  event of a scenario, in five survival modes, always a committed state, and the
  recovered service reads it and keeps working. Mutation check: removing the
  committed-table check from the allocator makes the campaign fail.
- [test_fsclient](../tests/test_fsclient.py) (7): the client library against the
  real service, a dishonest service, and the acceptance client end to end.
- [test_fs_profile](../tests/test_fs_profile.py) (5): the boot policy, rollback at
  every construction failure, a read-only root refusing to boot.
- [test_fs_probe](../tests/test_fs_probe.py) (2): the CPU probe's judgement,
  with a modelled run.

CPU: [probe_fs_cpu.py](../tests/probe_fs_cpu.py) boots the `fs` profile, waits for
the acceptance client ([client.m](../tests/programs/fs/client.m), which names a
failing step with its exit code), and snapshots the medium at every WRITE and
FLUSH command. Every snapshot must be a committed state of the reference run, in
order, and the final volume must hold exactly the expected files. **Not run.**
The author did not build WRM sources for this change (project rule), so neither
the compiled services nor the probe's emulator glue have executed.

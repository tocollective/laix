# G7. There is no application and storage layer above the microkernel

[Gap checklist](GAPS_CHECKLIST.md) · [Previous](GAP_06_EVIDENCE_AND_CI.md)

Date: 2026-10-07. Priority: **P3** (outside the kernel definition).

## Criterion

These items do not affect whether LA/IX is a microkernel. They decide
whether it is a usable general-purpose OS. All of them are user services,
not kernel features.

## Current state

- Storage can be written (2026-10-08, [FILESYSTEM.md](FILESYSTEM.md)): the kernel
  broker accepts WRITE and FLUSH under three-way authority, the Disk service has a
  sector stage, and the WFS1 service is a flat, crash-consistent filesystem. It
  runs in the `fs` profile only; Files remains a read-only service for the font
  and for the one-program loader volume. The `fs` profile is source accepted and
  has not run on a CPU.
- Program loading from storage exists ([FILES_LOADER.md](FILES_LOADER.md)), but it
  reads from Files, which serves one file, so there is one program per volume;
  choosing by name needs the loader to read from the filesystem service.
- There is a shell ([SHELL.md](SHELL.md)) and a networking path ([NETWORK.md](NETWORK.md)),
  both source accepted and not run on a CPU, in separate boot profiles (the task
  table has room for one or the other). No POSIX layer.
- No bulk-data path beyond 32-byte IPC chunks. Shared grants exist but are
  not used for I/O ([OPTIONAL_EXTENSIONS.md](OPTIONAL_EXTENSIONS.md)).

## Fix directions

Suggested order, each as a separate service with its own contract and
acceptance:

1. ~~Writable block path: extend the device contract ([G2](GAP_02_KERNEL_POLICY.md)),
   then a writable filesystem service with crash-consistency rules.~~ Done:
   [FILESYSTEM.md](FILESYSTEM.md). Source accepted; CPU run pending; not yet under the
   recovery supervisor.
2. ~~General loader reading ELF files from Files, with the bounded-copy,
   rollback and quota rules of the current catalog.~~ Mechanism done
   ([FILES_LOADER.md](FILES_LOADER.md)); the Exec service now selects a program by name
   from the filesystem ([SHELL.md](SHELL.md)). Source accepted; CPU run pending.
3. ~~A shell as an ordinary application over the console service.~~ Done
   ([SHELL.md](SHELL.md)), same status.
4. ~~Ethernet driver service through the device broker, then a minimal
   protocol stack service.~~ Done ([NETWORK.md](NETWORK.md)): a brokered card, a driver
   service and an ARP/IPv4/ICMP/UDP stack with ping and DNS lookup. No TCP. Source
   accepted; CPU run pending.
5. ~~A POSIX-like library layer only if applications need it.~~ Decided: not needed
   yet ([below](#decision-posix-like-library)).

## Decision: POSIX-like library

The condition was "only if applications need it". The applications that exist are
the shell and the programs Exec runs. A program's whole authority is one console
endpoint, so there is nothing for descriptors, `open`, `read`, `fork` or `exec`
to be a layer over: it can print and compute. What programs do need is small and
exists as plain libraries: formatted output ([text.m](../user/text.m)), file access
([fsclient.m](../user/services/fsclient.m)), program start
([execclient.m](../user/services/execclient.m)) and the network
([ipclient.m](../user/services/ipclient.m)). A POSIX layer built now would be a
thin and misleading skin over a single `write`.

It becomes needed when programs get more than the console: files or the keyboard.
That needs a way to give a loaded program several endpoints (the kernel's
configure call takes one; the start handle list used by boot-built tasks is a
boot-time mechanism) and a per-process descriptor table in user space. Revisit
then; the first step would be a library over the existing clients, not a new
service.

## Done when

Each service has its own readiness-matrix row, failure scenario (service dies
during an operation) and recovery behavior consistent with
[SERVICE_RECOVERY.md](SERVICE_RECOVERY.md).

Status (2026-10-08): the rows and failure scenarios exist for the filesystem
([FILESYSTEM.md](FILESYSTEM.md)), Exec and the shell ([SHELL.md](SHELL.md)) and the
network path ([NETWORK.md](NETWORK.md)); a crash-consistency campaign covers the
filesystem. **Recovery under the supervisor is not done:** none of the new services
runs in a supervised profile, so a death is observed by the next caller as
`-EPIPE` and the system does not replace the service. What state survives a death
is specified (committed filesystem state; nothing else), and the kernel side is
tested (a replacement Disk owner comes back read-only and reports dirty-at-regrant;
a dead net driver's card is stopped and its buffers freed). No G7 profile has run
on a CPU.

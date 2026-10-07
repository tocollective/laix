# Exec, the shell and the `shell` profile (G7)

[G7](GAP_07_APPLICATION_LAYER.md) · [Filesystem](FILESYSTEM.md) · [Files-backed loading](FILES_LOADER.md) · [Console service](CONSOLE_SERVICE.md)

Date: 2026-10-08. Status: **implemented; source accepted; the `shell` profile and
its probe are written but have not been run on a CPU, are not in CI and not in
the checked-in provenance.**

```sh
LAIX_CONSOLE=shell sh laix/build.sh          # builds the image; not run by the author
LAIX_CONSOLE=shell sh laix/run.sh            # type in the emulator window, read the UART
python3 laix/tests/probe_shell_cpu.py laix/build/shell.img laix/build/shell.map
```

## What runs

```text
              +-- Disk (writable extent) -- Fs ---+--------------+
keyboard -----+                                    |              |
   (raw HID)  +-- Shell ---- Exec (load authority)+              |
                   |             | (programs: console only)      |
                   +-------------+-------> Console server (UART transmit)
```

Five tasks, each a separate address space ([shell_bootstrap.m](../src/kernel/shell_bootstrap.m)):

| Task | Authority |
| --- | --- |
| Disk | the whole approved storage root, writable ([FILESYSTEM](FILESYSTEM.md#authority-who-may-write)); the disk IRQ |
| Fs | the Disk endpoint (send) and nothing else |
| Console | UART transmit; the existing checked console server of the `uart` profile |
| Exec | `IMAGE_LOAD_AUTHORITY`; send handles to Fs and the console |
| Shell | the raw keyboard broker; send handles to Fs, Exec and the console |

The number five is not arbitrary. The kernel has eight task slots and runtime
creators may not use the last two (reserved for recovery), so five boot tasks leave
exactly one slot for the program Exec loads. There is no separate Input service in
this profile for the same reason; the shell is the only keyboard reader.

**Start handles.** A start record carries one endpoint. Tasks that need more get
them in the first words of their data page, written by boot policy before they run:
magic `HNDL`, a count, then the handles ([starthandles.m](../user/starthandles.m)).
Exec's list is [Fs, console]; the shell's is [Fs, Exec, console]. The record
types stay as they were: the three servers use the checked service/console records,
Exec and the shell the runtime record that supervisors give children.

## Exec

[exec.m](../user/services/exec.m) answers one request, `EXEC_RUN` (32 bytes: name
of up to 16 bytes, an argument, a limit in seconds). It reads the file from Fs,
calls `SYS_TASK_LOAD` (which checks the image, snapshots it and charges Exec's
quota of four uncollected children), configures the child with **one** endpoint
(the console, send-only) and the argument, publishes it, and waits. The reply
carries the exit code, or the fault flag and trap cause; `-ETIMEDOUT` if the
limit expired and the child was terminated; `-ENOENT`, `-EINVAL` (not an image,
over 64 KiB, empty), `-ENFILE` (no frames, slot or quota) from the steps before.
Waiting polls with `yield` and then with one-second sleeps, so short programs
finish fast and long ones do not burn the CPU.

A program therefore has the authority Exec gave it and no more: it can print, and
it can compute. It cannot read files or the keyboard, and it cannot start
anything; the caller cannot widen that, and nor can the program. Programs that
need files wait for the library layer decided below.

## The shell

[shell.m](../user/apps/shell.m) is an ordinary application. It reads HID events
from the kernel broker, maps them with a US layout ([keymap.m](../user/keymap.m)),
edits one line (the console has no backspace, so an erase redraws the line with a
carriage return) and executes it.

| Command | Does |
| --- | --- |
| `help` | lists the commands |
| `ls` | name and size of every file |
| `cat NAME` | shows a file; bytes that cannot be shown print as `.` |
| `write NAME TEXT` | replaces (or creates) a file with the text and a newline, committed |
| `cp FROM TO` | copies a file of at most 8 KiB |
| `mv FROM TO` | copies and removes in **one commit**: the medium never shows both names or neither |
| `rm NAME` | removes a file, committed |
| `df` | data sectors, free sectors, generation, read-only / uncommitted markers |
| `sync` | commits |
| `echo TEXT` | prints |
| `run NAME [N]`, or `NAME [N]` | runs a program from the volume with argument N; prints `exit CODE` or `fault, cause C` |

Every message is at most 28 bytes per console call, made of printable ASCII, tab,
LF and CR ([text.m](../user/text.m)). A refusal prints `error: ...` and leaves the
volume as it was; `cp`/`mv` onto the same name is refused.

The volume ([build_shell_volume.py](../tools/build_shell_volume.py)) holds `motd`
and three programs from [user/bin](../user/bin): `hello` (prints its argument,
exits with it), `count` (prints 1 to N, exits N) and `spin` (never ends; try
`run spin` and wait out the 60-second limit).

## Limits

- The terminal is a UART: type in the emulator window, read the host terminal.
  There is no Screen in this profile: Screen's font storage owns the only disk
  extent the broker supports, and the filesystem needs it.
- The shell polls the keyboard with `yield`; it uses the CPU while idle.
- A program started by `run` blocks the shell until it exits or its limit ends.
  There is no job control and no interrupt key.
- Programs get the console endpoint only. No stdin, no files, no arguments beyond
  one number. Giving them more needs a library layer over a per-process system
  endpoint; that is the "POSIX-like library, if needed" item, which is not built.
- Loading a 37 KiB program reads it 16 bytes per call through Fs and Disk: about
  two to four seconds of machine time per run.
- Boot lifetime limits ([G4](GAP_04_FINITE_LIFETIMES.md)) apply: every request is a
  kernel call with finite reply namespaces.

## Evidence

Source: [test_exec](../tests/test_exec.py) (10, against the real filesystem
service and a scripted kernel), [test_shell](../tests/test_shell.py) (15, scripted
keystrokes, the real filesystem service, a scripted Exec; the medium is checked
after each change), [test_shell_profile](../tests/test_shell_profile.py) (5: who
holds which handle, what each handle reaches, authority per task, rollback at every
construction failure), [test_shell_probe](../tests/test_shell_probe.py) (4: the
probe's script, transcript patterns and volume judgement, run on a modelled
session).

CPU: [probe_shell_cpu.py](../tests/probe_shell_cpu.py) types ten commands with an
emulator input script (clock ticks, deterministic), reads the UART, and parses the
volume afterwards. **Not run.** The author did not build WRM sources for this
change (project rule): the compiled services, the console server's coexistence
with ELF tasks in one profile, the start handle list, `SYS_TASK_LOAD` from a
boot-constructed task with a runtime start record, and the timing of the key
script are all unexercised on a CPU.

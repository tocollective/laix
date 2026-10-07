#!/usr/bin/env python3
"""G7 CPU probe: the writable filesystem over the real Disk, DMA and IRQ path.

Needs the fs image (LAIX_CONSOLE=fs sh laix/build.sh). Input, Disk, the WFS1
service and its acceptance client (tests/programs/fs/client.m) run on the
emulated CPU against a private copy of the disk. The client checks every
service answer itself and names a failing step with its exit code. This probe
checks what the client cannot see:

* the medium: the final volume parses with tools/wfs.py and holds exactly the
  files the client leaves (build_fs_volume.FINAL_FILES), and nothing outside the
  approved storage root changed;
* crash consistency on the real device path: at every WRITE and FLUSH command
  the probe snapshots the medium (what a power loss at that instant would leave),
  and every snapshot must be one of the committed states of the reference run
  (fs_reference.py), in order;
* the kernel's records afterwards: all four services finished, no DMA page left
  pinned, no pending device operation.

Nothing is built. The emulator is driven through its monitor; the disk is the
probe's private copy, read back while the machine is paused.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys

from probe_boot import ready_monitor, require, Monitor
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import task_layout, uart_text
from run_ready import ROOT, check_layout
from test_kernel import LAIX
from source_m import LAYOUT as C

sys.path.insert(0, str(LAIX / 'tools'))
import build_fs_volume  # noqa: E402
import wfs  # noqa: E402
from fs_reference import committed_states  # noqa: E402

CLIENT_SLOT = 4                     # input, disk, fs, client
CONFIG = ('--ram', '2M', '--clock', '128M', '--deterministic')
MAX_STOPS = 100000
WRITE, FLUSH = C['DISK_WRITE'], C['DISK_FLUSH']


def volume_of(data):
    sectors = struct.unpack_from('<I', data, 4)[0]
    return sectors * 512


def run_image(data, map_path, emulator, rom, timeout):
    symbols = symbols_from_map(map_path)
    check_layout(symbols)
    size, fields = task_layout()
    boot_bytes = volume_of(data)
    with ready_monitor(data, emulator, rom, timeout, full_image=True, extra_args=CONFIG) as opened:
        monitor, process, stdout, stderr = opened
        transcript = [monitor.receive(), monitor.stop_at(symbols['taskStart'])]
        monitor.command(f"del 0x{symbols['taskStart']:X}")

        def field(slot, name):
            address = symbols['tasks'] + (slot - 1) * size + fields[name]
            return monitor.words(address & ~3, 1)[0]

        monitor.command(f"b 0x{symbols['taskFinish']:X}")
        monitor.command(f"b 0x{symbols['deviceSubmit']:X}")
        snapshots, submits, finished = [], {WRITE: 0, FLUSH: 0}, []
        for _ in range(MAX_STOPS):
            monitor.command('c')
            raw = monitor.command('r')
            registers = Monitor.registers(raw)
            if registers['pc'] == symbols['deviceSubmit']:
                # deviceSubmit(owner, offset, bytes, command, source): r1..r5. The
                # command has not been issued yet, so the file shows what a power
                # loss right now would leave behind.
                command = registers['r4']
                if command in (WRITE, FLUSH):
                    submits[command] += 1
                    snapshots.append((command, Path(monitor.disk).read_bytes()))
                continue
            transcript.append(raw)
            current = monitor.words(symbols['currentTask'], 1)[0]
            slot = monitor.words(current + fields['slot'], 1)[0]
            finished.append(slot)
            if slot == CLIENT_SLOT:
                break
        else:
            raise ValueError('the acceptance client did not finish')
        monitor.command(f"del 0x{symbols['taskFinish']:X}")
        monitor.command(f"del 0x{symbols['deviceSubmit']:X}")
        transcript.append(monitor.stop_at(symbols['taskKernelResume.idle']))
        exit_code = field(CLIENT_SLOT, 'exitCode')
        require(field(CLIENT_SLOT, 'state') == 3 and exit_code == 0,
                f'filesystem client failed: exit {exit_code} (the code names the step in tests/programs/fs/client.m)')
        final = Path(monitor.disk).read_bytes()
        pinned = monitor.words(symbols['service_devices__deviceBounce'], 1)[0]
        uart = uart_text(stdout)
        require('PANIC' not in uart, 'kernel panic during the filesystem run')
        require(pinned == 0, 'a DMA page is still pinned after the run')
    return dict(final=final, snapshots=snapshots, submits=submits, finished=finished,
                boot_bytes=boot_bytes), uart, '\n'.join(transcript)


def judge(result, original):
    boot = result['boot_bytes']
    states = committed_states()
    require(len(result['final']) == len(original), 'the disk changed size')
    require(result['final'][:boot] == original[:boot], 'the kernel image on the medium changed')
    volume = result['final'][boot:]
    table = wfs.current(volume)[1]
    wfs.check_table(table)
    require(wfs.files(volume) == build_fs_volume.FINAL_FILES, 'the final volume does not hold the expected files')
    require(states[-1] == build_fs_volume.FINAL_FILES, 'the reference run disagrees with the expected final files')
    # Every instant a command was about to be issued is a legal committed state,
    # and the states only move forward.
    position = 0
    for number, (command, image) in enumerate(result['snapshots']):
        require(image[:boot] == original[:boot], f'snapshot {number}: the kernel image changed')
        snapshot = image[boot:]
        try:
            files = wfs.files(snapshot)
            wfs.check_table(wfs.current(snapshot)[1])
        except wfs.FormatError as error:
            raise ValueError(f'snapshot {number} (command {command}) is not a valid volume: {error}')
        later = [i for i in range(position, len(states)) if states[i] == files]
        require(later, f'snapshot {number} (command {command}) is not a committed state at or after state {position}')
        position = later[0]
    return dict(snapshots=len(result['snapshots']), write_commands=result['submits'][WRITE],
                flush_commands=result['submits'][FLUSH], committed_states=len(states),
                generation=wfs.current(volume)[1]['generation'])


def probe(image, map_path, emulator, rom, timeout):
    paths = dict(image=image, map=map_path, emulator=emulator, rom=rom)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    data = image.read_bytes()
    result, uart, transcript = run_image(data, map_path, emulator, rom, timeout)
    report = dict(complete=False, sha256=hashes)
    report.update(judge(result, data))
    require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()),
            'probe changed input artifacts')
    report['complete'] = True
    return report, uart, transcript


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=LAIX / 'build/acceptance/fs')
    parser.add_argument('--timeout', type=float, default=1200)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report, uart, transcript = probe(args.image.resolve(), args.map.resolve(), args.emulator.resolve(),
                                         args.rom.resolve(), args.timeout)
        (args.log_dir / 'fs.uart.txt').write_text(uart)
        (args.log_dir / 'fs.monitor.txt').write_text(transcript)
    except (ValueError, OSError, subprocess.TimeoutExpired, wfs.FormatError) as error:
        report = dict(complete=False, error=str(error))
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' filesystem CPU: ' +
          (f"{report['snapshots']} medium snapshots at WRITE/FLUSH commands were all committed states, "
           f"{report['write_commands']} writes, {report['flush_commands']} flushes, final generation {report['generation']}"
           if report['complete'] else report['error']))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

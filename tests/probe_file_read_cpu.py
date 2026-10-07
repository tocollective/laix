#!/usr/bin/env python3
"""G1 CPU measurement: a 64 KiB read through the Files service, end to end.

Needs the file-read services image (LAIX_ACCEPTANCE_FILEREAD=1 LAIX_CONSOLE=services
sh laix/build.sh), which replaces only the Services acceptance application with
tests/programs/console/file_read_client.m; Input, Disk and Files are the ordinary
services. The client reads 4096 pieces of 16 bytes, the most Files returns. The
probe pauses at an empty marker function in the client and at the client's
exit (the Files service shares its user addresses, so an end marker would fire on
every request). The span covers the real client -> Files -> Disk -> DMA -> IRQ round trips, scheduling and idle, in
virtual CPU cycles at 128 MHz. Nothing is built and no memory is written.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from probe_boot import ready_monitor, require
from probe_limits_latency_cpu import counters, CLOCK
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import task_layout, uart_text
from run_ready import ROOT, check_layout
from test_kernel import LAIX

BYTES, PIECE = 65536, 16
CLIENT_SLOT = 4  # input, disk, files, application


def at_client(monitor, address, ptbr):
    """Services share the client's user addresses, so check whose breakpoint it is."""
    for _ in range(4096):
        stopped = monitor.stop_at(address)
        if monitor.registers(stopped)['ptbr'] == ptbr:
            monitor.command(f"del 0x{address:X}")
            return stopped
    raise ValueError(f'client did not reach marker {address:X}')


def probe(image, map_path, emulator, rom, timeout):
    paths = dict(image=image, map=map_path, emulator=emulator, rom=rom)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    symbols = symbols_from_map(map_path)
    check_layout(symbols)
    users = symbols_from_map(image.parent / 'services/simple-application.map')
    require('fileReadArmed' in users, 'not the file-read image: fileReadArmed is missing')
    size, fields = task_layout()

    def field(slot, name):
        return monitor.words(symbols['tasks'] + (slot - 1) * size + fields[name], 1)[0]

    config = ('--ram', '2M', '--clock', '128M', '--deterministic')
    with ready_monitor(image.read_bytes(), emulator, rom, timeout, full_image=True, extra_args=config) as opened:
        monitor, process, stdout, stderr = opened
        transcript = [monitor.receive(), monitor.stop_at(symbols['taskStart'])]
        monitor.command(f"del 0x{symbols['taskStart']:X}")
        ptbr = field(CLIENT_SLOT, 'ptbr')
        transcript.append(at_client(monitor, users['fileReadArmed'], ptbr))
        start = counters(monitor.command('r'))
        # Only the client ever exits before the machine goes idle.
        transcript.append(monitor.stop_at(symbols['taskFinish']))
        end = counters(monitor.command('r'))
        transcript.append(monitor.stop_at(symbols['taskKernelResume.idle']))
        require(field(CLIENT_SLOT, 'state') == 3 and field(CLIENT_SLOT, 'exitCode') == 0,
                f'client failed: exit {field(CLIENT_SLOT, "exitCode")}')
        uart = uart_text(stdout)
        require('PANIC' not in uart, 'kernel panic during the read')
    require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()),
            'probe changed input artifacts')
    cycles, retired = end[0] - start[0], end[1] - start[1]
    reads = BYTES // PIECE
    return dict(complete=True, bytes=BYTES, piece_bytes=PIECE, reads=reads, cycles=cycles, retired=retired,
                seconds_at_128MHz=round(cycles / CLOCK, 6), cycles_per_read=round(cycles / reads),
                ms_per_read=round(cycles * 1000 / CLOCK / reads, 6),
                bytes_per_second=round(BYTES * CLOCK / cycles), config=' '.join(config), sha256=hashes), uart, '\n'.join(transcript)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=LAIX / 'build/acceptance/file-read')
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report, uart, transcript = probe(args.image.resolve(), args.map.resolve(), args.emulator.resolve(),
                                         args.rom.resolve(), args.timeout)
        (args.log_dir / 'file-read.uart.txt').write_text(uart)
        (args.log_dir / 'file-read.monitor.txt').write_text(transcript)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report = dict(complete=False, error=str(error))
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' file read CPU: ' +
          (f"{report['bytes']} bytes in {report['reads']} reads, {report['cycles']} cycles "
           f"({report['seconds_at_128MHz']} s), {report['ms_per_read']} ms per read, {report['bytes_per_second']} B/s"
           if report['complete'] else report['error']))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

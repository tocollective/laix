#!/usr/bin/env python3
"""Run the supervisor on existing image/emulator bytes, without building."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from probe_boot import ready_monitor, require
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import task_layout, uart_text
from run_ready import ROOT, check_layout
from test_kernel import LAIX, check_m
from source_m import LAYOUT as C


def probe(image, map_path, emulator, rom, timeout=30):
    paths = dict(image=image, map=map_path, emulator=emulator, rom=rom)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    symbols = symbols_from_map(map_path)
    check_layout(symbols)
    size, fields = task_layout()
    types = check_m(LAIX / 'src/task/control.m')
    event = next(module.scope['TaskEvent'].type for module in types if 'TaskEvent' in module.scope and module.scope['TaskEvent'].type is not None)
    controls = next(module.scope['TaskControl'].type for module in types if 'TaskControl' in module.scope)
    with ready_monitor(image.read_bytes(), emulator, rom, timeout, full_image=True) as opened:
        monitor, process, stdout, stderr = opened
        transcript = [monitor.receive(), monitor.stop_at(symbols['taskStart'])]
        monitor.command(f"del 0x{symbols['taskStart']:X}")
        transcript.append(monitor.stop_at(symbols['taskKernelResume.idle']))
        def field(slot, name):
            address = symbols['tasks'] + (slot - 1) * size + fields[name]
            word = monitor.words(address & ~3, 1)[0]
            return (word >> ((address & 3) * 8)) & 255 if name == 'reaped' else word
        require(field(1, 'state') == 3 and field(1, 'exitCode') == 0 and field(1, 'reaped') & 255 == 1,
                'supervisor failed or did not finish/reap')
        require(all(field(slot, 'state') == 0 and field(slot, 'directory') == 0 and
                    field(slot, 'kernelStackBottom') == 0 for slot in range(2, 9)),
                'runtime resources/slots survived completion')
        require(monitor.words(symbols['task__readyCount'], 1) == [0] and
                monitor.words(symbols['currentTask'], 1) == [symbols['idleTask']],
                'scheduler did not finish on the selected idle stack')
        require(monitor.words(symbols['taskHistoryCount'], 1) == [26], 'lost completion history')
        events = [monitor.words(symbols['taskHistory'] + i * event.size, event.size // 4) for i in range(26)]
        refs = set()
        for code, record in enumerate(events[:24]):
            require(record[:5] == [code << 8 | 2, 2, 3, code, C['TASK_EVENT_RECLAIMED']],
                    f'wrong normal completion {code}: {record}')
            refs.add(record[0])
        fault = events[24]
        require(fault[:6] == [24 << 8 | 2, 2, 3, 3, 5, 3] and fault[7] == 1,
                f'fault diagnostics missing: {fault}')
        require(len(refs) == 24, 'task generation did not advance on reuse')
        require(events[25][:5] == [1, 1, 3, 0, 4], 'wrong supervisor completion')
        require(all(monitor.words(symbols['taskControls'] + i * controls.size +
                                  controls.field('reference').offset, 1) == [0] for i in range(16)),
                'supervisor leaked task capabilities')
        uart = uart_text(stdout)
        require('PANIC' not in uart, 'kernel panic in runtime task path')
    require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()),
            'probe changed input artifacts')
    return dict(complete=True, lifetimes=25, sha256=hashes), uart, '\n'.join(transcript)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=LAIX / 'build/acceptance/runtime-tasks')
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report, uart, transcript = probe(args.image, args.map, args.emulator.resolve(), args.rom.resolve(), args.timeout)
        (args.log_dir / 'supervisor.uart.txt').write_text(uart)
        (args.log_dir / 'supervisor.monitor.txt').write_text(transcript)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report = dict(complete=False, error=str(error))
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' runtime task CPU: ' +
          ('24 exits, one fault and supervisor collection' if report['complete'] else report['error']))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

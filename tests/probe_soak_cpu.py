#!/usr/bin/env python3
"""G6 soak CPU probe: thousands of task lifetimes on existing image/emulator bytes.

Needs the soak image (LAIX_CONSOLE=soak sh laix/build.sh). The scenario is
src/kernel/soak_bootstrap.asm: a user supervisor creates, runs, faults or
exits, collects and reuses a child SOAK_ROUNDS times and checks every round's
SYS_LIFETIME report and completion event itself. This probe checks what the
supervisor cannot see: the kernel's state at the end, after the whole run.
The task history is a 32-entry ring, so only its last 32 events are compared.
Nothing is built and no counter is written.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from probe_boot import ready_monitor, require
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import task_layout, uart_text
from run_ready import ROOT, check_layout
from test_kernel import LAIX, check_m
from source_m import LAYOUT as C

HISTORY = 32
GEN_MAX = 0x7FFFFF
FAULT_PERIOD = 8


def scenario_rounds():
    """The round count is the scenario's own constant, bound by the source manifest."""
    match = re.search(r'^SOAK_ROUNDS = (\d+)', (LAIX / 'src/kernel/soak_bootstrap.asm').read_text(), re.M)
    require(match is not None, 'scenario does not define SOAK_ROUNDS')
    rounds = int(match[1])
    require(rounds % FAULT_PERIOD == 0 and rounds > HISTORY, f'unusable SOAK_ROUNDS {rounds}')
    return rounds


def probe(image, map_path, emulator, rom, timeout=600):
    rounds = scenario_rounds()
    paths = dict(image=image, map=map_path, emulator=emulator, rom=rom)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    symbols = symbols_from_map(map_path)
    check_layout(symbols)
    size, fields = task_layout()
    types = check_m(LAIX / 'src/task/control.m')
    event = next(module.scope['TaskEvent'].type for module in types
                 if 'TaskEvent' in module.scope and module.scope['TaskEvent'].type is not None)
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
                f'soak supervisor failed or did not finish/reap: exit {field(1, "exitCode")}')
        require(all(field(slot, 'state') == 0 and field(slot, 'directory') == 0 and
                    field(slot, 'kernelStackBottom') == 0 for slot in range(2, 9)),
                'runtime resources/slots survived the soak')
        # Finite lifetimes: every round spent one generation of the one slot
        # that served, and nothing else moved (no reply identity, no other slot).
        require(field(2, 'id') >> 8 == rounds - 1 and field(2, 'id') & 255 == 2,
                f'slot 2 did not spend exactly {rounds} reference generations: {field(2, "id"):#x}')
        require(all(field(slot, 'id') == 0 for slot in range(3, 9)), 'a second slot was used')
        require(all(field(slot, 'ipcCallGeneration') == 0 for slot in range(1, 9)),
                'a reply namespace advanced without any IPC')
        require(monitor.words(symbols['task__readyCount'], 1) == [0] and
                monitor.words(symbols['currentTask'], 1) == [symbols['idleTask']],
                'scheduler did not finish on the selected idle stack')
        require(all(monitor.words(symbols['taskControls'] + i * controls.size +
                                  controls.field('reference').offset, 1) == [0] for i in range(16)),
                'soak leaked task capabilities')
        # The ring holds the last 32 completions: 31 children and the supervisor.
        require(monitor.words(symbols['taskHistoryCount'], 1) == [HISTORY], 'history ring is not saturated')
        head = monitor.words(symbols['taskHistoryHead'], 1)[0]
        events = [monitor.words(symbols['taskHistory'] + ((head + i) % HISTORY) * event.size, event.size // 4)
                  for i in range(HISTORY)]
        for index, record in enumerate(events[:-1]):
            code = rounds - (HISTORY - 1) + index
            if code % FAULT_PERIOD == FAULT_PERIOD - 1:
                require(record[:6] == [code << 8 | 2, 2, 3, 3, 5, 3] and record[7] == 1,
                        f'wrong fault completion {code}: {record}')
            else:
                require(record[:5] == [code << 8 | 2, 2, 3, code, C['TASK_EVENT_RECLAIMED']],
                        f'wrong normal completion {code}: {record}')
        require(events[-1][:5] == [1, 1, 3, 0, 4], 'wrong supervisor completion')
        uart = uart_text(stdout)
        require('PANIC' not in uart, 'kernel panic during the soak')
    require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()),
            'probe changed input artifacts')
    return dict(complete=True, lifetimes=rounds, faults=rounds // FAULT_PERIOD, sha256=hashes), uart, '\n'.join(transcript)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=LAIX / 'build/acceptance/soak')
    parser.add_argument('--timeout', type=float, default=600)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report, uart, transcript = probe(args.image, args.map, args.emulator.resolve(), args.rom.resolve(), args.timeout)
        (args.log_dir / 'soak.uart.txt').write_text(uart)
        (args.log_dir / 'soak.monitor.txt').write_text(transcript)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report = dict(complete=False, error=str(error))
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' soak CPU: ' +
          (f"{report['lifetimes']} task lifetimes ({report['faults']} faults), exact generation accounting, nothing leaked"
           if report['complete'] else report['error']))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

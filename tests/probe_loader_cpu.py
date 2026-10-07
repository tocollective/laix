#!/usr/bin/env python3
"""G1 CPU probe: children loaded from the storage volume through Disk and Files.

Needs the loader image (LAIX_CONSOLE=loader sh laix/build.sh). The kernel image
holds Disk, Files and the loader client only; the child (tests/programs/loader/
hello.m) exists as bytes on the appended storage volume. The user loader reads
those bytes over the real DMA/IRQ path, calls SYS_TASK_LOAD and checks every
outcome itself (user/services/loader.m: its exit code names the failing step).
This probe checks what the loader cannot: the kernel's records afterwards, the
eleven child completions in the history ring, and that a tampered volume is refused.
Nothing is built; tampering edits only the probe's private copy of the disk.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess

from probe_boot import ready_monitor, require, Monitor
from probe_limits_latency_cpu import counters, CLOCK, SECTION_BUDGET
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import task_layout, uart_text
from run_ready import ROOT, check_layout
from test_kernel import LAIX, check_m
from source_m import LAYOUT as C

LOADER_SLOT = 4                     # input, disk, files, loader
GOOD_CODES = [45, 54, 63, 72]       # (5 + argument) * 9
FAULT_CAUSE = 3                     # the approved unaligned-read fault
# Tampering with the volume must make the first load fail: exit code 10.
TAMPERS = {
    'magic': lambda elf: bytes([elf[0] ^ 1]) + elf[1:],
    'entry': lambda elf: elf[:24] + struct.pack('<I', 0x41FFF000) + elf[28:],
    'segment': lambda elf: elf[:52 + 8] + struct.pack('<I', 0) + elf[52 + 12:],
}
CONFIG = ('--ram', '2M', '--clock', '128M', '--deterministic')


def run_image(data, map_path, emulator, rom, timeout, tamper=None):
    symbols = symbols_from_map(map_path)
    check_layout(symbols)
    size, fields = task_layout()
    types = check_m(LAIX / 'src/task/control.m')
    event = next(module.scope['TaskEvent'].type for module in types
                 if 'TaskEvent' in module.scope and module.scope['TaskEvent'].type is not None)
    controls = next(module.scope['TaskControl'].type for module in types if 'TaskControl' in module.scope)
    if tamper:
        sectors = struct.unpack_from('<I', data, 4)[0]
        volume = sectors * 512
        elf_bytes = data[volume:]
        data = data[:volume] + TAMPERS[tamper](elf_bytes)
    with ready_monitor(data, emulator, rom, timeout, full_image=True, extra_args=CONFIG) as opened:
        monitor, process, stdout, stderr = opened
        transcript = [monitor.receive(), monitor.stop_at(symbols['taskStart'])]
        monitor.command(f"del 0x{symbols['taskStart']:X}")

        def field(slot, name):
            address = symbols['tasks'] + (slot - 1) * size + fields[name]
            return monitor.words(address & ~3, 1)[0]

        # Run until the loader itself finishes; children finish on the way. Each
        # SYS_TASK_LOAD is timed from its entry to the trap return that follows:
        # the syscall runs with IRQs excluded, so nothing else executes between.
        restore = symbols['trapEntry.restore']
        monitor.command(f"b 0x{symbols['taskFinish']:X}")
        monitor.command(f"b 0x{symbols['taskRuntimeLoad']:X}")
        finished, samples, started = [], [], None
        for _ in range(600):
            monitor.command('c')
            raw = monitor.command('r')
            pc = Monitor.registers(raw)['pc']
            if pc == symbols['taskRuntimeLoad']:
                require(started is None, 'nested SYS_TASK_LOAD')
                started = counters(raw)
                monitor.command(f"b 0x{restore:X}")
            elif pc == restore:
                require(started is not None, 'unexpected trap return')
                samples.append(counters(raw)[0] - started[0])
                started = None
                monitor.command(f"del 0x{restore:X}")
            else:
                transcript.append(raw)
                current = monitor.words(symbols['currentTask'], 1)[0]
                slot = monitor.words(current + fields['slot'], 1)[0]
                finished.append(slot)
                if slot == LOADER_SLOT:
                    break
        else:
            raise ValueError('loader did not finish')
        monitor.command(f"del 0x{symbols['taskFinish']:X}")
        monitor.command(f"del 0x{symbols['taskRuntimeLoad']:X}")
        transcript.append(monitor.stop_at(symbols['taskKernelResume.idle']))
        exit_code = field(LOADER_SLOT, 'exitCode')
        children = [slot for slot in finished if slot != LOADER_SLOT]
        history = monitor.words(symbols['taskHistoryCount'], 1)[0]
        head = monitor.words(symbols['taskHistoryHead'], 1)[0]
        events = [monitor.words(symbols['taskHistory'] + ((head - history + i) % 32) * event.size, event.size // 4)
                  for i in range(history)]
        result = dict(exit_code=exit_code, children=len(children), events=events, load_cycles=samples)
        if tamper is None:
            require(field(LOADER_SLOT, 'state') == 3 and exit_code == 0, f'loader failed: exit {exit_code}')
            require(len(children) == 11 and history == 12, f'expected eleven children: {children}')
            codes = [record[3] for record in events[:-1]]
            expected = GOOD_CODES + [FAULT_CAUSE] + GOOD_CODES + [GOOD_CODES[3]] * 2
            require(codes == expected, f'wrong child exit codes: {codes}')
            flags = [record[4] for record in events[:-1]]
            require(all(flag == C['TASK_EVENT_RECLAIMED'] for index, flag in enumerate(flags) if index != 4) and
                    flags[4] == C['TASK_EVENT_RECLAIMED'] | C['TASK_EVENT_FAULT'],
                    f'wrong completion flags: {flags}')
            require(events[4][5] == FAULT_CAUSE, 'fault diagnostics missing')
            require(events[-1][:5] == [field(LOADER_SLOT, 'id'), LOADER_SLOT, 3, 0, 4], 'wrong loader completion')
            # 11 successful loads, 1 quota refusal and 5 refused requests that
            # reach the syscall (the oversized, 16-byte and unmapped ones are
            # also counted: every call returns, none stalls).
            require(len(samples) == 11 + 1 + 5 and max(samples) < SECTION_BUDGET,
                    f'load syscall count or time out of bounds: {len(samples)} calls, max {max(samples, default=0)}')
            require(all(field(slot, 'state') == 0 and field(slot, 'directory') == 0 and
                        field(slot, 'kernelStackBottom') == 0 for slot in range(5, 9)),
                    'a loaded child kept runtime resources')
            require(all(monitor.words(symbols['taskControls'] + i * controls.size +
                                      controls.field('reference').offset, 1) == [0] for i in range(16)),
                    'loader leaked task capabilities')
        else:
            require(exit_code == 10 and not children and history == 1,
                    f'tampered volume ({tamper}) was not refused: exit {exit_code}, children {children}')
        uart = uart_text(stdout)
        require('PANIC' not in uart, 'kernel panic while loading')
    return result, uart, '\n'.join(transcript)


def probe(image, map_path, emulator, rom, timeout, tampers):
    paths = dict(image=image, map=map_path, emulator=emulator, rom=rom)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    data = image.read_bytes()
    report = dict(complete=False, sha256=hashes)
    good, uart, transcript = run_image(data, map_path, emulator, rom, timeout)
    report['loaded_children'] = good['children']
    report['load_syscalls'] = len(good['load_cycles'])
    report['load_cycles_max'] = max(good['load_cycles'])
    report['load_cycles_min'] = min(good['load_cycles'])
    report['load_ms_max_at_128MHz'] = round(max(good['load_cycles']) * 1000 / CLOCK, 6)
    report['load_budget_cycles'] = SECTION_BUDGET
    report['child_exit_codes'] = [record[3] for record in good['events'][:-1]]
    report['tampered'] = {}
    for name in tampers:
        refused, _, _ = run_image(data, map_path, emulator, rom, timeout, tamper=name)
        report['tampered'][name] = dict(loader_exit_code=refused['exit_code'], children=refused['children'])
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
    parser.add_argument('--log-dir', type=Path, default=LAIX / 'build/acceptance/loader')
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--tamper', action='append', choices=sorted(TAMPERS), default=None,
                        help='refusal cases to run (default: all)')
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report, uart, transcript = probe(args.image.resolve(), args.map.resolve(), args.emulator.resolve(),
                                         args.rom.resolve(), args.timeout, args.tamper or sorted(TAMPERS))
        (args.log_dir / 'loader.uart.txt').write_text(uart)
        (args.log_dir / 'loader.monitor.txt').write_text(transcript)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report = dict(complete=False, error=str(error))
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' loader CPU: ' +
          (f"{report['loaded_children']} children loaded from the volume via Files, "
           f"{len(report['tampered'])} tampered volumes refused, SYS_TASK_LOAD max "
           f"{report['load_cycles_max']} cycles ({report['load_ms_max_at_128MHz']} ms)"
           if report['complete'] else report['error']))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Inspect the dedicated memory image on existing emulator/ROM bytes."""
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


def probe(image, map_path, emulator, rom, timeout=30):
    user_map = image.parent / 'memory-user/memory.map'
    paths = dict(image=image, map=map_path, user_map=user_map, emulator=emulator, rom=rom)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    symbols = symbols_from_map(map_path)
    check_layout(symbols)
    size, fields = task_layout()
    types = {name: module.scope[name].type
             for module in check_m(LAIX / 'src/mm/runtime.m')
             for name in ('MemoryBudget', 'MemoryRegion', 'MemorySpace', 'MemoryGrant') if name in module.scope and module.scope[name].type is not None}
    with ready_monitor(image.read_bytes(), emulator, rom, timeout, full_image=True) as opened:
        monitor, process, stdout, stderr = opened
        transcript = [monitor.receive(), monitor.stop_at(symbols['taskStart'])]
        monitor.command(f"del 0x{symbols['taskStart']:X}")
        # Require a real timer interrupt to reach taskTick during user work.
        transcript.append(monitor.stop_at(symbols['taskTick']))
        monitor.command(f"del 0x{symbols['taskTick']:X}")
        user_symbols = symbols_from_map(user_map)
        transcript.append(monitor.stop_at(user_symbols['memoryExecute']))
        directory = monitor.words(symbols['tasks'] + fields['directory'], 1)[0]
        kernel_directory = monitor.words(symbols['mmu__kernelPageDirectory'], 1)[0]
        virtual = 0x603FF000
        for page in range(2):
            address = virtual + page * 4096
            table = monitor.words(directory + address // 0x400000 * 4, 1)[0] & ~4095
            leaf = monitor.words(table + address // 4096 % 1024 * 4, 1)[0]
            require(leaf & 31 == 27, 'user code is not RX')
            physical = leaf & ~4095
            identity_entry = monitor.words(kernel_directory + physical // 0x400000 * 4, 1)[0]
            require(monitor.words(directory + physical // 0x400000 * 4, 1)[0] == identity_entry,
                    'user root does not share the protected kernel identity table')
            identity_table = identity_entry & ~4095
            require(monitor.words(identity_table + physical // 4096 % 1024 * 4, 1)[0] & 31 == 3,
                    'supervisor alias remains writable or executable')
        monitor.command(f"del 0x{user_symbols['memoryExecute']:X}")
        transcript.append(monitor.stop_at(symbols['taskKernelResume.idle']))

        def field(slot, name):
            address = symbols['tasks'] + (slot - 1) * size + fields[name]
            word = monitor.words(address & ~3, 1)[0]
            return (word >> ((address & 3) * 8)) & 255 if name == 'reaped' else word

        for slot in (1, 2):
            require(field(slot, 'state') == 3 and field(slot, 'exitCode') == 0 and field(slot, 'reaped') == 1,
                    f'memory user {slot} failed: code={field(slot, "exitCode")}')
            require(field(slot, 'directory') == 0 and field(slot, 'kernelStackBottom') == 0,
                    f'memory user {slot} leaked address space or stack')
        require(all(field(slot, 'state') == 0 for slot in range(3, 9)), 'runtime child survived')
        for variable, typename, count, fieldname in (
                ('memoryBudgets', 'MemoryBudget', 8, 'owner'),
                ('memoryRegions', 'MemoryRegion', 64, 'owner'),
                ('memorySpaces', 'MemorySpace', 32, 'caller'),
                ('memoryGrants', 'MemoryGrant', 64, 'lender')):
            typ = types[typename]
            require(all(monitor.words(symbols[variable] + i * typ.size + typ.field(fieldname).offset, 1) == [0]
                        for i in range(count)), f'leaked {variable}')
        require(monitor.words(symbols['memoryOrphanPages'], 1) == [0], 'leaked orphan frame charges')
        ram_end = monitor.words(symbols['kernelRamEnd'], 1)[0]
        reserved = monitor.words(symbols['kernelReservedEnd'], 1)[0]
        records = monitor.words(symbols['memory__pagePurposes'], 1)[0]
        purposes = monitor.words(records + reserved // 4096 * 4, (ram_end - reserved) // 4096)
        require(all(purpose in (0, 3, 4, 7) for purpose in purposes), 'leaked user/data frames')
        free = monitor.words(symbols['memoryFreePages'], 1)[0]
        require(free == purposes.count(0), 'free-frame accounting differs from page ledger')
        uart = uart_text(stdout)
        require('PANIC' not in uart, 'kernel panic in memory path')
    require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()),
            'probe changed input artifacts')
    return dict(complete=True, heap_lifetimes=20, ipc_round_trips=20,
                timer_preemption=True, supervisor_alias_ro=True, loader_publication=True, exhaustion_recovery=True, sha256=hashes), uart, '\n'.join(transcript)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=LAIX / 'build/acceptance/runtime-memory')
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report, uart, transcript = probe(args.image, args.map, args.emulator.resolve(), args.rom.resolve(), args.timeout)
        (args.log_dir / 'memory.uart.txt').write_text(uart)
        (args.log_dir / 'memory.monitor.txt').write_text(transcript)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report = dict(complete=False, error=str(error))
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' runtime memory CPU: ' +
          ('heap, IPC, timer, W^X, loader and reclamation' if report['complete'] else report['error']))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

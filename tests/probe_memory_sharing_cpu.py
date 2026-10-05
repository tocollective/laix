#!/usr/bin/env python3
"""Both sharing death orders on an existing dedicated image and emulator."""
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
    user_map = image.parent / 'sharing-user/sharing.map'
    paths = dict(image=image, map=map_path, user_map=user_map, emulator=emulator, rom=rom)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    symbols = symbols_from_map(map_path)
    user_symbols = symbols_from_map(user_map)
    check_layout(symbols)
    size, fields = task_layout()
    types = {name: module.scope[name].type
             for module in check_m(LAIX / 'src/mm/sharing.m')
             for name in ('MemoryRegion', 'MemoryGrant', 'MemoryBudget', 'MemorySpace') if name in module.scope and module.scope[name].type is not None}
    outcomes, logs = {}, {}
    for order, marker in ((0, 'sharingOwnerGone'), (1, 'sharingBorrowerGone')):
        label = 'owner-first' if order == 0 else 'borrower-first'
        with ready_monitor(image.read_bytes(), emulator, rom, timeout, full_image=True) as opened:
            monitor, process, stdout, stderr = opened
            transcript = [monitor.receive(), monitor.stop_at(symbols['taskStart'])]

            def field(slot, name):
                address = symbols['tasks'] + (slot - 1) * size + fields[name]
                word = monitor.words(address & ~3, 1)[0]
                return (word >> ((address & 3) * 8)) & 255 if name == 'reaped' else word

            def record(variable, typ, index, name):
                address = symbols[variable] + index * typ.size + typ.field(name).offset
                word = monitor.words(address & ~3, 1)[0]
                return (word >> ((address & 3) * 8)) & 255 if name in ('detached', 'open') else word

            if order:
                # Only temporary-machine startup data changes; image bytes and
                # executable instructions remain identical for the two cases.
                for slot in range(1, 4):
                    monitor.command(f"wp 0x{field(slot, 'bootPage') + 36:X} {10 + slot - 1}")
            monitor.command(f"del 0x{symbols['taskStart']:X}")
            transcript.append(monitor.stop_at(user_symbols[marker]))
            region_type = types['MemoryRegion']
            rows = [i for i in range(64) if record('memoryRegions', region_type, i, 'owner') == 1]
            require(len(rows) == 1, f'{label}: lost shared owner ledger')
            index = rows[0]
            require(record('memoryRegions', region_type, index, 'count') == 2, 'wrong shared region size')
            pages = monitor.words(symbols['memoryRegions'] + index * region_type.size +
                                  region_type.field('pages').offset, 2)
            owners = monitor.words(symbols['memory__pageOwners'], 1)[0]
            references = monitor.words(symbols['memory__pageReferences'], 1)[0]

            def available(page):
                word = monitor.words(symbols['memory__pageBitmap'] + page // 4096 // 32 * 4, 1)[0]
                return not bool(word & (1 << (page // 4096 % 32)))

            for page in pages:
                require(not available(page), f'{label}: frame reused after first death')
                require(monitor.words(owners + page // 4096 * 4, 1) == [1], 'allocation owner changed')
                require(monitor.words(references + page // 4096 * 4, 1) == [2 if order == 0 else 1],
                        'mapping/lease references are not conserved')
            require(monitor.words(pages[0], 1) == [0x12345678] and
                    monitor.words(pages[1], 1) == [0x87654321], 'shared data changed during teardown')
            require(record('memoryRegions', region_type, index, 'detached') == (order == 0),
                    'wrong orphan state after first death')
            require(monitor.words(symbols['memoryOrphanPages'], 1) == [2 if order == 0 else 0],
                    'wrong orphan charge after first death')
            if order == 0:
                require(field(1, 'id') == 257 and field(1, 'state') == 5,
                        'original owner slot was not reused as an unpublished replacement')
                require(record('memoryBudgets', types['MemoryBudget'], 0, 'owner') == 257,
                        'replacement did not get its own budget generation')
                directory = field(2, 'directory')
                for i, page in enumerate(pages):
                    virtual = 0x60000000 + i * 4096
                    table = monitor.words(directory + virtual // 0x400000 * 4, 1)[0] & ~4095
                    leaf = monitor.words(table + virtual // 4096 % 1024 * 4, 1)[0]
                    require(leaf & ~4095 == page and leaf & 31 == 23, 'borrower lost shared mapping')
            else:
                require(field(2, 'reaped') == 1 and field(2, 'directory') == 0,
                        'first borrower was not physically reaped')
                require(field(1, 'id') == 1 and field(1, 'directory') != 0, 'owner did not survive borrower')
            monitor.command(f"del 0x{user_symbols[marker]:X}")
            transcript.append(monitor.stop_at(user_symbols['sharingBothGone']))
            for page in pages:
                require(available(page), f'{label}: last borrower/owner did not release frame')
                require(monitor.words(owners + page // 4096 * 4, 1) == [0], 'released frame still owned')
                require(monitor.words(references + page // 4096 * 4, 1) == [0], 'released frame still pinned')
            require(monitor.words(symbols['memoryOrphanPages'], 1) == [0], 'orphan budget was not refunded')
            require(record('memoryRegions', region_type, index, 'owner') == 0, 'region row survived last reference')
            monitor.command(f"del 0x{user_symbols['sharingBothGone']:X}")
            transcript.append(monitor.stop_at(symbols['taskKernelResume.idle']))
            for slot in (2, 3):
                require(field(slot, 'state') == 3 and field(slot, 'reaped') == 1 and field(slot, 'exitCode') == 0,
                        f'{label}: user {slot} failed: code={field(slot, "exitCode")}')
            require(field(1, 'state') == 0, 'owner/replacement slot remained live')
            for variable, typename, count, name in (
                    ('memoryBudgets', 'MemoryBudget', 8, 'owner'),
                    ('memoryRegions', 'MemoryRegion', 64, 'owner'),
                    ('memoryGrants', 'MemoryGrant', 64, 'lender'),
                    ('memorySpaces', 'MemorySpace', 32, 'caller')):
                require(all(record(variable, types[typename], i, name) == 0 for i in range(count)),
                        f'{label}: leaked {variable}')
            ram_end = monitor.words(symbols['kernelRamEnd'], 1)[0]
            reserved = monitor.words(symbols['kernelReservedEnd'], 1)[0]
            purposes = monitor.words(monitor.words(symbols['memory__pagePurposes'], 1)[0] + reserved // 4096 * 4,
                                     (ram_end - reserved) // 4096)
            require(all(purpose in (0, 3, 4, 7) for purpose in purposes), 'leaked user frames')
            require(monitor.words(symbols['memoryFreePages'], 1) == [purposes.count(0)], 'wrong free count')
            uart = uart_text(stdout)
            require('PANIC' not in uart, 'kernel panic in sharing path')
        outcomes[label] = dict(complete=True, original_frames=pages,
                               owner_slot_reuse=order == 0, no_premature_reuse=True)
        logs[label] = dict(uart=uart, monitor='\n'.join(transcript))
    require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()),
            'probe changed input artifacts')
    return dict(complete=True, cases=outcomes, sha256=hashes), logs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=LAIX / 'build/acceptance/memory-sharing')
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report, logs = probe(args.image, args.map, args.emulator.resolve(), args.rom.resolve(), args.timeout)
        for label, log in logs.items():
            for kind, contents in log.items():
                (args.log_dir / f'{label}.{kind}.txt').write_text(contents)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report = dict(complete=False, error=str(error))
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' sharing CPU: ' +
          ('both death orders, owner slot reuse and final release' if report['complete'] else report['error']))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

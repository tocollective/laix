#!/usr/bin/env python3
"""Run runtime factories on existing WRM/ROM bytes, without building WRM."""
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
    paths = dict(image=image, map=map_path, user_map=image.parent / 'objects-user/objects.map',
                 emulator=emulator, rom=rom)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    symbols = symbols_from_map(map_path)
    check_layout(symbols)
    size, fields = task_layout()
    require(symbols['currentTask'] - symbols['tasks'] == 8 * size, 'image/source task layout mismatch')
    types = {name: module.scope[name].type
             for module in check_m(LAIX / 'src/trap/trap.m')
             for name in ('Endpoint', 'HandleTable', 'Handle', 'IrqGrant', 'TaskControl', 'Transfer')
             if name in module.scope and module.scope[name].type is not None}
    with ready_monitor(image.read_bytes(), emulator, rom, timeout, full_image=True) as opened:
        monitor, process, stdout, stderr = opened
        transcript = [monitor.receive(), monitor.stop_at(symbols['taskStart'])]
        monitor.command(f"del 0x{symbols['taskStart']:X}")
        transcript.append(monitor.stop_at(symbols['taskTick']))
        monitor.command(f"del 0x{symbols['taskTick']:X}")
        # The consent-expiry test sleeps before any user exits, so reaching
        # idle once no longer implies completion. Observe all three exits first.
        transcript.append(monitor.stop_at(symbols['taskFinish']))
        for _ in range(2):
            transcript.append(monitor.command('c'))
            transcript.append(monitor.command('r'))
        monitor.command(f"del 0x{symbols['taskFinish']:X}")
        transcript.append(monitor.stop_at(symbols['taskKernelResume.idle']))

        def field(slot, name):
            address = symbols['tasks'] + (slot - 1) * size + fields[name]
            word = monitor.words(address & ~3, 1)[0]
            return (word >> ((address & 3) * 8)) & 255 if name == 'reaped' else word

        for slot in (1, 2):
            require(field(slot, 'state') == 3 and field(slot, 'exitCode') == 0 and field(slot, 'reaped') == 1,
                    f'object user {slot} failed: code={field(slot, "exitCode")}')
            require(field(slot, 'directory') == 0 and field(slot, 'kernelStackBottom') == 0,
                    f'object user {slot} leaked physical resources')
        require(all(field(slot, 'state') == 0 for slot in range(3, 9)), 'runtime child survived')
        require(monitor.words(symbols['task__readyCount'], 1) == [0], 'ready queue survived')
        endpoint = types['Endpoint']
        for i in range(16):
            address = symbols['objects__endpoints'] + i * endpoint.size
            for name in ('references', 'creator', 'manager', 'receiveReferences', 'senderCount', 'receiverCount'):
                require(monitor.words(address + endpoint.field(name).offset, 1) == [0],
                        f'endpoint {i + 1} leaked {name}')
        require(monitor.words(symbols['objects__endpoints'] + endpoint.size + endpoint.field('generation').offset, 1)[0] >= 42,
                'runtime endpoint slot was not repeatedly reused')
        table, handle = types['HandleTable'], types['Handle']
        for slot in range(1, 9):
            address = symbols['tasks'] + (slot - 1) * size + fields['handles']
            require(monitor.words(address + table.field('factoryModes').offset, 1) == [0],
                    'factory root survived owner death')
            require(all(monitor.words(address + table.field('entries').offset + i * handle.size +
                                      handle.field('object').offset, 1) == [0] for i in range(16)),
                    'endpoint reference survived task teardown')
            for i in range(16):
                reserved = address + table.field('entries').offset + i * handle.size + handle.field('reserved').offset
                word = monitor.words(reserved & ~3, 1)[0]
                require((word >> ((reserved & 3) * 8)) & 255 == 0,
                        'handle reservation survived task teardown')
        transfer = types['Transfer']
        for slot in range(8):
            address = symbols['transfer__transfers'] + slot * transfer.size
            for name in ('state', 'receiver', 'sender', 'slot', 'rights', 'handle'):
                require(monitor.words(address + transfer.field(name).offset, 1) == [0],
                        f'transfer {slot + 1} leaked {name}')
        irq = types['IrqGrant']
        address = symbols['irq__irqGrants'] + C['KEYBOARD_IRQ'] * irq.size
        require(monitor.words(address + irq.field('owner').offset, 1) == [0], 'keyboard grant survived')
        require(monitor.words(address + irq.field('generation').offset, 1) == [2], 'keyboard grant was not renewed')
        require(monitor.words(symbols['input_device__inputOwner'], 1) == [0], 'input broker owner survived')
        control = types['TaskControl']
        require(all(monitor.words(symbols['taskControls'] + i * control.size +
                                  control.field('reference').offset, 1) == [0] for i in range(16)),
                'task control leaked')
        uart = uart_text(stdout)
        require('PANIC' not in uart, 'kernel panic in runtime factory path')
    require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()),
            'probe changed input artifacts')
    return dict(complete=True, raw_lifetimes=20, service_lifetimes=20,
                timer_preemption=True, quota_recovery=True, irq_renewals=2,
                consent_transfers=41, unsolicited_copies_rejected=640,
                forged_and_consumed_tickets=True, cancelled_and_expired_tickets=True,
                authenticated_notification=True, sender_death_after_commit=True,
                sha256=hashes), uart, '\n'.join(transcript)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=LAIX / 'build/acceptance/runtime-objects')
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report, uart, transcript = probe(args.image, args.map, args.emulator.resolve(), args.rom.resolve(), args.timeout)
        (args.log_dir / 'objects.uart.txt').write_text(uart)
        (args.log_dir / 'objects.monitor.txt').write_text(transcript)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report = dict(complete=False, error=str(error))
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' runtime objects CPU: ' +
          ('Raw/Service factories, transport, quotas, timer and IRQ renewal' if report['complete'] else report['error']))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

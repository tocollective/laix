#!/usr/bin/env python3
"""G4 CPU probe: a supervisor replaces a client before its reply namespace ends.

Needs the lifetime scenario image (LAIX_RECOVERY_FIXTURES=lifetime
LAIX_CONSOLE=recovery sh laix/build.sh) on the existing WRM binary and ROM.
The probe seeds one counter through the monitor, at a breakpoint where the
client exists but is unpublished, because user mode cannot and the kernel never
resets or sets a counter. Everything after that is the scenario's own code and
the kernel's syscall 75: it reads the remaining lifetime, retires the client
while the reserve is left, constructs a replacement that must land in another
namespace, and the replacement is served from a fresh counter.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from recovery_provenance import source_manifest, artifacts, digest
from probe_boot import ready_monitor, require
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import task_layout, uart_text
from run_ready import ROOT, check_layout
from test_kernel import LAIX, check_m

GEN_MAX = 0x7FFFFF
RESERVE = 4096          # LIFETIME_REPLY_RESERVE in src/arch/wrm081632/defs.m
SEEDED_REMAINING = RESERVE + 2
CLIENT_SLOT = 2
TASK_CREATED, TASK_DEAD = 5, 3


def stop_at_supervisor(monitor, transcript, address, ptbr):
    """The scenario image also runs as the client, so check whose marker it is."""
    for attempt in range(64):
        stopped = monitor.stop_at(address)
        transcript.append(stopped)
        if monitor.registers(stopped)['ptbr'] == ptbr:
            monitor.command(f"del 0x{address:X}")
            return
    raise ValueError(f'supervisor did not reach marker {address:X}')


def probe(image, map_path, emulator, rom, timeout=60):
    build_record = json.loads((image.parent / 'recovery.provenance.json').read_text())
    require(build_record.get('fixtures') == 'lifetime',
            'CPU acceptance requires LAIX_RECOVERY_FIXTURES=lifetime')
    require(build_record['sources'] == source_manifest(), 'source/build provenance mismatch; rebuild LA/IX recovery')
    require(build_record['artifacts'] == artifacts(), 'artifact/build provenance mismatch')
    hashes = {name: digest(path) for name, path in dict(image=image, map=map_path, emulator=emulator, rom=rom).items()}
    symbols = symbols_from_map(map_path)
    users = symbols_from_map(image.parent / 'recovery-user/policy.map')
    for marker in ('lifetimeArmed', 'lifetimeReplaced', 'lifetimeDone'):
        require(marker in users, f'scenario image lacks {marker}')
    check_layout(symbols)
    size, fields = task_layout()
    require(symbols['currentTask'] - symbols['tasks'] == 8 * size, 'task image/source layout mismatch')
    types = {name: module.scope[name].type for module in check_m(LAIX / 'src/trap/trap.m')
             for name in ('Endpoint', 'TaskControl', 'ServiceEntry', 'IrqGrant')
             if name in module.scope and module.scope[name].type is not None}

    def slot_address(slot, name):
        return symbols['tasks'] + (slot - 1) * size + fields[name]

    with ready_monitor(image.read_bytes(), emulator, rom, timeout, full_image=True) as opened:
        monitor, process, stdout, stderr = opened
        transcript = [monitor.receive(), monitor.stop_at(symbols['taskStart'])]
        monitor.command(f"del 0x{symbols['taskStart']:X}")
        transcript.append(monitor.stop_at(symbols['taskTick']))
        monitor.command(f"del 0x{symbols['taskTick']:X}")
        supervisor_ptbr = monitor.words(slot_address(1, 'ptbr'), 1)[0]

        def word(slot, name):
            return monitor.words(slot_address(slot, name), 1)[0]

        # 1. Client constructed, not yet published: seed its reply namespace.
        stop_at_supervisor(monitor, transcript, users['lifetimeArmed'], supervisor_ptbr)
        require(word(CLIENT_SLOT, 'state') == TASK_CREATED, 'client is not the unpublished slot-2 task')
        require(word(CLIENT_SLOT, 'ipcCallGeneration') == 0, 'client namespace was not fresh')
        seeded = GEN_MAX - SEEDED_REMAINING
        transcript.append(monitor.command(f"wp 0x{slot_address(CLIENT_SLOT, 'ipcCallGeneration'):X} {seeded}"))
        require(word(CLIENT_SLOT, 'ipcCallGeneration') == seeded, 'monitor write did not land')

        # 2. Client retired and collected, replacement published.
        stop_at_supervisor(monitor, transcript, users['lifetimeReplaced'], supervisor_ptbr)
        old = word(CLIENT_SLOT, 'ipcCallGeneration')
        remaining_at_retirement = GEN_MAX - old
        require(0 < remaining_at_retirement <= RESERVE,
                f'client was not replaced inside the reserve: {remaining_at_retirement} calls left')
        require(old > seeded, 'client made no calls')
        require(word(CLIENT_SLOT, 'state') == 0, 'retired client slot is not empty')
        replacement_slot = next((slot for slot in range(3, 9) if word(slot, 'state') in (1, 2, 4)), None)
        require(replacement_slot is not None, 'no live replacement')
        require(word(replacement_slot, 'ipcCallGeneration') < 8, 'replacement namespace was not fresh')

        # 3. The replacement is served, then retired; the old counter is untouched.
        stop_at_supervisor(monitor, transcript, users['lifetimeDone'], supervisor_ptbr)
        require(word(CLIENT_SLOT, 'ipcCallGeneration') == old, 'retired namespace counter changed')
        served = word(replacement_slot, 'ipcCallGeneration')
        require(20 <= served < 1000, f'replacement admitted an unexpected number of calls: {served}')

        transcript.append(monitor.stop_at(symbols['taskKernelResume.idle']))
        require(word(1, 'state') == TASK_DEAD and word(1, 'exitCode') == 0, f'scenario failed: exit {word(1, "exitCode")}')
        for slot in range(2, 9):
            require(word(slot, 'state') == 0 and word(slot, 'directory') == 0 and word(slot, 'kernelStackBottom') == 0,
                    f'live runtime resources at slot {slot}')
        # Slots are never reset: only the two used namespaces advanced.
        counters = {slot: word(slot, 'ipcCallGeneration') for slot in range(1, 9)}
        require(all(value == 0 for slot, value in counters.items() if slot not in (CLIENT_SLOT, replacement_slot)),
                f'unexpected namespace use: {counters}')
        for typ, base, count, names in [
                ('Endpoint', 'objects__endpoints', 16, ('references', 'creator', 'manager', 'receiveReferences', 'senderCount', 'receiverCount')),
                ('TaskControl', 'taskControls', 16, ('reference',)),
                ('ServiceEntry', 'recovery__serviceEntries', 8, ('owner', 'root', 'reference')),
                ('IrqGrant', 'irq__irqGrants', 32, ('owner',))]:
            layout = types[typ]
            for row in range(count):
                for name in names:
                    require(monitor.words(symbols[base] + row * layout.size + layout.field(name).offset, 1) == [0],
                            f'{typ} {row} leaked {name}')
        require(monitor.words(symbols['task__readyCount'], 1) == [0], 'ready queue survived')
        history = monitor.words(symbols['taskHistoryCount'], 1)[0]
        require(history == 3, f'expected client, replacement and supervisor completions: {history}')
        event = next(module.scope['TaskEvent'].type for module in check_m(LAIX / 'src/task/control.m')
                     if 'TaskEvent' in module.scope and module.scope['TaskEvent'].type)
        events = [monitor.words(symbols['taskHistory'] + i * event.size, event.size // 4) for i in range(history)]
        require(not any(record[4] & 1 for record in events), 'a task faulted')
        require(sum(bool(record[4] & 2) for record in events) == 2, 'expected two terminated clients')
        require(all(record[4] & 4 for record in events), 'unreclaimed completion')
        uart = uart_text(stdout)
        require('PANIC' not in uart, 'kernel panic')
    require(source_manifest() == build_record['sources'] and artifacts() == build_record['artifacts'],
            'provenance changed during CPU run')
    return dict(complete=True, seeded_remaining=SEEDED_REMAINING, reserve=RESERVE,
                remaining_at_retirement=remaining_at_retirement, client_slot=CLIENT_SLOT,
                replacement_slot=replacement_slot, replacement_calls=served,
                eoverflow_seen=False, final_live_children=0, sha256=hashes,
                build=build_record), uart, '\n'.join(transcript)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--log-dir', type=Path, default=LAIX / 'build/acceptance/lifetime')
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report, uart, transcript = probe(args.image, args.map, args.emulator.resolve(), args.rom.resolve(), args.timeout)
        (args.log_dir / 'lifetime.uart.txt').write_text(uart)
        (args.log_dir / 'lifetime.monitor.txt').write_text(transcript)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report = dict(complete=False, error=str(error))
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' lifetime CPU: ' +
          ('client replaced inside the reserve, replacement served from a fresh namespace'
           if report['complete'] else report['error']))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

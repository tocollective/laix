#!/usr/bin/env python3
"""Supervised display recovery on existing WRM/ROM executable bytes (G3).

Fixture image: four real Screen faults mid-frame, five Screen generations, each
rendering through a fresh bitmap storage service. Production image: the normal
supervisor boots Screen, bitmap storage, Echo and a status client; no crash hook.
"""
import argparse
import json
import tempfile
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from screen_recovery_provenance import source_manifest, artifacts, digest
from probe_boot import ready_monitor, require
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import task_layout, uart_text
from run_ready import ROOT, check_layout
from test_kernel import LAIX, check_m

PITCH = 640
VRAM_BYTES = 0x400000
GLYPH_ROWS = 16
GLYPH_COLUMNS = 16 # two 8-pixel cells: "G" and a generation digit


# source/snapshot.c snap_video: after the 'VID ' tag, 8-byte numbers (control, mode,
# start, frame, 256 palette words, ticks, 13 drawing registers, 6 line words, 4 cursor
# words), 1-byte flags (palette index, vblank, busy, done), the 8200-byte line buffer,
# then the VRAM. The next part ('DISK') starts right after it.
VIDEO_HEADER_BYTES = 8 * (4 + 256 + 1 + 13 + 6 + 4) + 4 + 8200


def frame(monitor, rows):
    """Pixels of the first rows of the 640x480 8bpp frame. The monitor cannot read
    the VRAM window (its bus read is a fetch, which the VRAM refuses), so a machine
    snapshot is parsed instead; its layout is checked by the part that follows."""
    with tempfile.TemporaryDirectory(prefix='laix-vram-') as directory:
        path = Path(directory)/'machine.snap'
        response = monitor.command(f"save {path}")
        require(path.exists(), f'monitor did not save a snapshot: {response}')
        data = path.read_bytes()
    video = data.find(b'VID ')
    require(video > 0, 'snapshot lacks the video part')
    start = video + 4 + VIDEO_HEADER_BYTES
    require(data[start + VRAM_BYTES:start + VRAM_BYTES + 4] == b'DISK', 'snapshot video layout changed')
    return data[start:start + rows * PITCH]


def stop_once(monitor, address):
    stopped = monitor.stop_at(address)
    monitor.command(f"del 0x{address:X}")
    return stopped


def stop_from(monitor, address, low, high, limit=4096):
    """Stop at `address` when it was called from [low, high) of the same image.

    The images share one load address, so the client's marker VA is also code in
    the Screen, Echo and bitmap tasks; a breakpoint there fires in each of them.
    The return address in ra tells the client's call from those executions."""
    monitor.command(f"b 0x{address:X}")
    for _ in range(limit):
        monitor.command("c")
        raw = monitor.command("r")
        registers = monitor.registers(raw)
        require(registers['pc'] == address, f"did not stop at {address:08X}")
        if low <= registers['r31'] < high:
            monitor.command(f"del 0x{address:X}")
            return raw
    raise ValueError(f"no call of {address:08X} from the client within {limit} stops")


def stop_in_service(monitor, symbols, types, fields, address, name, limit=4096):
    """Stop at `address` when the running task is the one published as service `name`.

    Another task of the profile can execute the same virtual address (the images
    share one load address), and a breakpoint there fires in all of them."""
    entry = types['ServiceEntry']
    monitor.command(f"b 0x{address:X}")
    for _ in range(limit):
        monitor.command("c")
        raw = monitor.command("r")
        require(monitor.registers(raw)['pc'] == address, f"did not stop at {address:08X}")
        task = monitor.words(symbols['currentTask'], 1)[0]
        identity = monitor.words(task + fields['id'], 1)[0]
        for row in range(8):
            base = symbols['recovery__serviceEntries'] + row * entry.size
            if (identity != 0 and monitor.words(base + entry.field('reference').offset, 1)[0] == identity and
                    monitor.words(base + entry.field('name').offset, 1)[0] == name):
                monitor.command(f"del 0x{address:X}")
                return raw
    raise ValueError(f"service {name} never reached {address:08X} within {limit} stops")


def cell(pixels, column, rows=GLYPH_ROWS, width=8):
    return bytes(pixels[row * PITCH + column + x] for row in range(rows) for x in range(width))


def table_clean(monitor, symbols, types):
    for typ, base, count, names in [
        ('Endpoint', 'objects__endpoints', 16, ('references', 'creator', 'manager', 'receiveReferences', 'senderCount', 'receiverCount')),
        ('TaskControl', 'taskControls', 16, ('reference',)),
        ('ServiceEntry', 'recovery__serviceEntries', 8, ('owner', 'root', 'reference')),
        ('IrqGrant', 'irq__irqGrants', 32, ('owner',)),
        ('ResourceGrant', 'mmu__resourceGrants', 8, ('directory',))]:
        layout = types[typ]
        for row in range(count):
            for name in names:
                require(monitor.words(symbols[base] + row * layout.size + layout.field(name).offset, 1) == [0],
                        f'{typ} {row} leaked {name}')


def probe(image, map_path, emulator, rom, timeout=120):
    build_record = json.loads((image.parent/'screenrecovery.provenance.json').read_text())
    require(build_record.get('fixtures') == 'scenario', 'CPU acceptance requires LAIX_SCREEN_RECOVERY_FIXTURES=scenario')
    require(build_record['sources'] == source_manifest(), 'source/build provenance mismatch; rebuild the profile')
    require(build_record['artifacts'] == artifacts(), 'artifact/build provenance mismatch')
    hashes = {name: digest(path) for name, path in dict(image=image, map=map_path, emulator=emulator, rom=rom).items()}
    symbols = symbols_from_map(map_path)
    users = symbols_from_map(image.parent/'screen-recovery-user/policy.map')
    screen = symbols_from_map(image.parent/'screen-recovery-user/screen.map')
    check_layout(symbols)
    size, fields = task_layout()
    types = {name: module.scope[name].type for module in check_m(LAIX/'src/trap/trap.m')
             for name in ('Endpoint', 'TaskControl', 'ServiceEntry', 'IrqGrant')
             if name in module.scope and module.scope[name].type is not None}
    types['ResourceGrant'] = next(module.scope['ResourceGrant'].type for module in check_m(LAIX/'src/mm/mmu.m')
                                  if 'ResourceGrant' in module.scope and module.scope['ResourceGrant'].type)
    require('taskCapacity' in symbols, 'task image has no sized task table')
    with ready_monitor(image.read_bytes(), emulator, rom, timeout, full_image=True) as opened:
        monitor, process, stdout, stderr = opened
        transcript = [monitor.receive(), monitor.stop_at(symbols['taskStart'])]
        monitor.command(f"del 0x{symbols['taskStart']:X}")
        transcript.append(monitor.stop_at(symbols['taskTick']))
        monitor.command(f"del 0x{symbols['taskTick']:X}")
        supervisor_ptbr = monitor.words(symbols['tasks'] + fields['ptbr'], 1)[0]
        # Each generation: the new Screen has rendered the client's write (stopped
        # inside Screen before its frame wait), then the client saw the reply.
        renders = []
        for generation in range(1, 6):
            stopped = stop_in_service(monitor, symbols, types, fields, screen['videoFrame'], 1)
            transcript.append(stopped)
            require(monitor.registers(stopped)['ptbr'] != supervisor_ptbr, 'Screen frame stop in the supervisor')
            pixels = frame(monitor, GLYPH_ROWS + 16)
            renders.append(dict(
                letter=cell(pixels, 0), digit=cell(pixels, 8),
                below=pixels[GLYPH_ROWS * PITCH:], right=b''.join(pixels[row*PITCH + GLYPH_COLUMNS:(row+1)*PITCH]
                                                                   for row in range(GLYPH_ROWS))))
            stopped = stop_from(monitor, users['screenRendered'], users['scenario__clientMain'],
                                users['screenScenarioMain'])
            transcript.append(stopped)
            marker = monitor.registers(stopped)['r1']
            require(marker == generation, f'render marker out of order at {generation}: saw {marker}')
        for generation, render in enumerate(renders, 1):
            require(any(render['letter']) and any(render['digit']), f'generation {generation} rendered no glyphs')
            require(not any(render['below']) and not any(render['right']),
                    f'generation {generation} frame holds stale pixels outside its text')
            require(render['letter'] == renders[0]['letter'], f'generation {generation} letter differs')
        require(len({render['digit'] for render in renders}) == 5, 'generations rendered the same digit')
        for attempt in range(256):
            stopped = monitor.stop_at(users['recoveryDone'])
            transcript.append(stopped)
            if monitor.registers(stopped)['ptbr'] == supervisor_ptbr:
                break
        else:
            raise ValueError('supervisor did not reach its recovery completion marker')
        monitor.command(f"del 0x{users['recoveryDone']:X}")
        transcript.append(monitor.stop_at(symbols['taskKernelResume.idle']))
        def field(slot, name):
            address = symbols['tasks']+(slot-1)*size+fields[name]
            word = monitor.words(address & ~3, 1)[0]
            return (word >> ((address & 3)*8)) & 255 if name == 'reaped' else word
        require(field(1,'state') == 3 and field(1,'exitCode') == 0 and field(1,'reaped') == 1,
                f'supervisor did not finish: {field(1,"exitCode")}')
        for slot in range(2, 9):
            require(field(slot,'state') == 0 and field(slot,'directory') == 0 and field(slot,'kernelStackBottom') == 0,
                    f'live runtime resources at slot {slot}')
        table_clean(monitor, symbols, types)
        for name in ('service_devices__deviceBounce', 'service_devices__screenOwner', 'task__readyCount'):
            require(monitor.words(symbols[name], 1) == [0], f'{name} not at baseline')
        history = monitor.words(symbols['taskHistoryCount'], 1)[0]
        require(history == 14, f'missing display/storage/echo/client/peer/supervisor completion: {history}')
        event = next(module.scope['TaskEvent'].type for module in check_m(LAIX/'src/task/control.m') if 'TaskEvent' in module.scope and module.scope['TaskEvent'].type)
        events = [monitor.words(symbols['taskHistory']+i*event.size, event.size//4) for i in range(history)]
        require(sum(bool(record[4]&1) for record in events) == 4, 'expected four actual Screen faults')
        require(all(record[4]&4 for record in events), 'unreclaimed completion')
        require(not any(record[4]&8 for record in events), 'a completion was quarantined')
        irq = types['IrqGrant']
        for line, name in ((5, 'video'), (3, 'disk')):
            require(monitor.words(symbols['irq__irqGrants']+line*irq.size+irq.field('generation').offset, 1) == [5],
                    f'{name} IRQ not renewed five times')
        uart = uart_text(stdout)
        require('PANIC' not in uart, 'kernel panic')
    require(source_manifest() == build_record['sources'] and artifacts() == build_record['artifacts'],
            'provenance changed during CPU run')
    return dict(complete=True, generations=5, actual_faults=4, rendered_generations=5,
                distinct_frames=5, stale_screen_handles=True, unrelated_peer=True, echo_unaffected=True,
                final_live_children=0, dma_pin_released=True, resource_ledger_empty=True,
                sha256=hashes, build=build_record), uart, '\n'.join(transcript)


def production_probe(image, map_path, emulator, rom, timeout=120):
    build_record = json.loads((image.parent/'screenrecovery.provenance.json').read_text())
    require(build_record.get('fixtures') == '', 'production smoke requires fixtures disabled')
    require(build_record['sources'] == source_manifest() and build_record['artifacts'] == artifacts(),
            'production build/source provenance mismatch')
    symbols = symbols_from_map(map_path)
    screen = symbols_from_map(image.parent/'screen-recovery-user/screen.map')
    size, fields = task_layout()
    hashes = {name: digest(path) for name, path in dict(image=image, map=map_path, emulator=emulator, rom=rom).items()}
    with ready_monitor(image.read_bytes(), emulator, rom, timeout, full_image=True) as opened:
        monitor, process, stdout, stderr = opened
        transcript = [monitor.receive(), monitor.stop_at(symbols['taskStart'])]
        monitor.command(f"del 0x{symbols['taskStart']:X}")
        # Two status writes: the client draws "LA/IX nnnnnn gG" once per second.
        for _ in range(3):
            transcript.append(monitor.stop_at(screen['videoFrame']))
        monitor.command(f"del 0x{screen['videoFrame']:X}")
        pixels = frame(monitor, GLYPH_ROWS)
        require(sum(1 for byte in pixels if byte) > 100, 'production Screen drew nothing')
        for slot in range(1, 6):
            state = monitor.words(symbols['tasks']+(slot-1)*size+fields['state'], 1)[0]
            require(state in (1, 2, 4), f'production task {slot} died: {state}')
        require(monitor.words(symbols['taskHistoryCount'], 1) == [0], 'production task died')
        uart = uart_text(stdout)
        require('PANIC' not in uart, 'production panic')
    require(source_manifest() == build_record['sources'] and artifacts() == build_record['artifacts'],
            'production provenance changed during CPU run')
    return dict(complete=True, production=True, status_line_drawn=True, live_tasks=5,
                sha256=hashes, build=build_record), uart, '\n'.join(transcript)


def watchdog_probe(image, map_path, emulator, rom, timeout=180):
    """The production supervisor and client against a Screen that really faults on
    its third request in generations 1-3: watchdog and report paths, no scripted retire."""
    build_record = json.loads((image.parent/'screenrecovery.provenance.json').read_text())
    require(build_record.get('fixtures') == 'watchdog', 'watchdog acceptance requires LAIX_SCREEN_RECOVERY_FIXTURES=watchdog')
    require(build_record['sources'] == source_manifest() and build_record['artifacts'] == artifacts(),
            'watchdog build/source provenance mismatch')
    symbols = symbols_from_map(map_path)
    screen = symbols_from_map(image.parent/'screen-recovery-user/screen.map')
    size, fields = task_layout()
    irq = next(module.scope['IrqGrant'].type for module in check_m(LAIX/'src/trap/trap.m')
               if 'IrqGrant' in module.scope and module.scope['IrqGrant'].type)
    video = symbols['irq__irqGrants'] + 5*irq.size + irq.field('generation').offset
    hashes = {name: digest(path) for name, path in dict(image=image, map=map_path, emulator=emulator, rom=rom).items()}
    with ready_monitor(image.read_bytes(), emulator, rom, timeout, full_image=True) as opened:
        monitor, process, stdout, stderr = opened
        transcript = [monitor.receive(), monitor.stop_at(symbols['taskStart'])]
        monitor.command(f"del 0x{symbols['taskStart']:X}")
        renders = {}
        # Every stop is a completed render by some Screen generation; wait for the fourth,
        # which has no fault hook, to serve three requests.
        for stop in range(64):
            transcript.append(monitor.stop_at(screen['videoFrame']))
            generation = monitor.words(video, 1)[0]
            renders[generation] = renders.get(generation, 0) + 1
            if renders.get(4, 0) >= 3:
                break
        else:
            raise ValueError(f'fourth Screen generation did not serve three requests: {renders}')
        monitor.command(f"del 0x{screen['videoFrame']:X}")
        require(sorted(renders) == [1, 2, 3, 4], f'unexpected Screen generations: {renders}')
        require(renders[1] == renders[2] == renders[3] == 2, f'each faulting generation serves two requests: {renders}')
        pixels = frame(monitor, GLYPH_ROWS)
        require(sum(1 for byte in pixels if byte) > 100, 'replacement Screen drew nothing')
        for slot in range(1, 6):
            state = monitor.words(symbols['tasks']+(slot-1)*size+fields['state'], 1)[0]
            require(state in (1, 2, 4), f'task {slot} is not live: {state}')
        history = monitor.words(symbols['taskHistoryCount'], 1)[0]
        require(history == 6, f'expected three Screen and three bitmap completions: {history}')
        event = next(module.scope['TaskEvent'].type for module in check_m(LAIX/'src/task/control.m') if 'TaskEvent' in module.scope and module.scope['TaskEvent'].type)
        events = [monitor.words(symbols['taskHistory']+i*event.size, event.size//4) for i in range(history)]
        require(sum(bool(record[4]&1) for record in events) == 3, 'expected three real Screen faults')
        require(all(record[4]&4 for record in events), 'unreclaimed completion')
        require(monitor.words(symbols['service_devices__deviceBounce'], 1) == [0], 'DMA pin survived a replacement')
        uart = uart_text(stdout)
        require('PANIC' not in uart, 'kernel panic')
    require(source_manifest() == build_record['sources'] and artifacts() == build_record['artifacts'],
            'provenance changed during CPU run')
    return dict(complete=True, watchdog=True, generations=4, actual_faults=3, replacement_rendered=True,
                live_tasks=5, sha256=hashes, build=build_record), uart, '\n'.join(transcript)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT/'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT/'bin/firmware.rom')
    parser.add_argument('--production', action='store_true')
    parser.add_argument('--watchdog', action='store_true')
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--log-dir', type=Path, default=LAIX/'build/acceptance/screen-recovery')
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    stem = 'production' if args.production else 'watchdog' if args.watchdog else 'recovery'
    try:
        run = production_probe if args.production else watchdog_probe if args.watchdog else probe
        report, uart, transcript = run(args.image, args.map, args.emulator.resolve(), args.rom.resolve(), args.timeout)
        (args.log_dir/f'{stem}.uart.txt').write_text(uart)
        (args.log_dir/f'{stem}.monitor.txt').write_text(transcript)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report = dict(complete=False, error=str(error))
    (args.log_dir/('production.json' if args.production else 'watchdog.json' if args.watchdog else 'results.json')).write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
    print(('PASS' if report['complete'] else 'FAIL')+' screen recovery CPU: ' + (
        ('production status line and five live tasks' if args.production else
         'watchdog recovery of three real Screen faults, fourth generation rendering' if args.watchdog else
         'five Screen generations, four mid-frame faults, each replacement rendering') if report['complete'] else report['error']))
    return 0 if report['complete'] else 1
if __name__ == '__main__':
    raise SystemExit(main())

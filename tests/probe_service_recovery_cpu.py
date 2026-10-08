#!/usr/bin/env python3
"""Natural user supervision/reconnection on existing WRM/ROM executable bytes."""
import argparse
import hashlib
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


def probe(image, map_path, emulator, rom, timeout=60):
    build_record = json.loads((image.parent/'recovery.provenance.json').read_text())
    require(build_record.get('fixtures') is True, 'CPU acceptance requires LAIX_RECOVERY_FIXTURES=1')
    require(build_record['sources'] == source_manifest(), 'source/build provenance mismatch; rebuild LA/IX recovery')
    require(build_record['artifacts'] == artifacts(), 'artifact/build provenance mismatch')
    hashes = {name: digest(path) for name, path in dict(image=image,map=map_path,emulator=emulator,rom=rom).items()}
    symbols = symbols_from_map(map_path)
    users = symbols_from_map(image.parent/'recovery-user/policy.map')
    check_layout(symbols)
    size, fields = task_layout()
    types = {name: module.scope[name].type for module in check_m(LAIX/'src/trap/trap.m')
             for name in ('Endpoint','TaskControl','ServiceEntry','IrqGrant')
             if name in module.scope and module.scope[name].type is not None}
    require('taskCapacity' in symbols, 'task image has no sized task table')
    with ready_monitor(image.read_bytes(), emulator, rom, timeout, full_image=True) as opened:
        monitor, process, stdout, stderr = opened
        transcript = [monitor.receive(), monitor.stop_at(symbols['taskStart'])]
        monitor.command(f"del 0x{symbols['taskStart']:X}")
        transcript.append(monitor.stop_at(symbols['taskTick']))
        monitor.command(f"del 0x{symbols['taskTick']:X}")
        supervisor_ptbr = monitor.words(symbols['tasks'] + fields['ptbr'], 1)[0]
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
        for slot in range(2,9):
            require(field(slot,'state') == 0 and field(slot,'directory') == 0 and field(slot,'kernelStackBottom') == 0,
                    f'live runtime resources at slot {slot}')
        for typ, base, count, names in [
            ('Endpoint','objects__endpoints',16,('references','creator','manager','receiveReferences','senderCount','receiverCount')),
            ('TaskControl','taskControls',16,('reference',)),
            ('ServiceEntry','recovery__serviceEntries',8,('owner','root','reference')),
            ('IrqGrant','irq__irqGrants',32,('owner',))]:
            layout = types[typ]
            for row in range(count):
                for name in names:
                    require(monitor.words(symbols[base]+row*layout.size+layout.field(name).offset,1) == [0],
                            f'{typ} {row} leaked {name}')
        require(monitor.words(symbols['service_devices__deviceBounce'],1) == [0], 'DMA bounce survived')
        require(monitor.words(symbols['task__readyCount'],1) == [0], 'ready queue survived')
        history = monitor.words(symbols['taskHistoryCount'],1)[0]
        require(history == 18, f'missing service/client/peer/supervisor completion: {history}')
        event = next(module.scope['TaskEvent'].type for module in check_m(LAIX/'src/task/control.m') if 'TaskEvent' in module.scope and module.scope['TaskEvent'].type)
        events = [monitor.words(symbols['taskHistory']+i*event.size,event.size//4) for i in range(history)]
        require(sum(bool(record[4]&1) for record in events) == 8, 'expected eight actual user faults')
        require(all(record[4]&4 for record in events), 'unreclaimed completion')
        irq = types['IrqGrant']
        require(monitor.words(symbols['irq__irqGrants']+3*irq.size+irq.field('generation').offset,1) == [5], 'Disk IRQ not renewed five times')
        uart = uart_text(stdout)
        require('PANIC' not in uart, 'kernel panic')
    require(source_manifest() == build_record['sources'] and artifacts() == build_record['artifacts'], 'provenance changed during CPU run')
    return dict(complete=True, generations=5, actual_faults=8, successful_reads=5,
                explicit_reconnects=5, timer_preemption=True, unrelated_peer=True,
                stale_handles=True, final_live_children=0, sha256=hashes,
                build=build_record), uart, '\n'.join(transcript)


def production_probe(image, map_path, emulator, rom, timeout=60, window=None):
    build_record = json.loads((image.parent/'recovery.provenance.json').read_text())
    require(build_record.get('fixtures') is False, 'production smoke requires fixtures disabled')
    require(build_record['sources'] == source_manifest() and build_record['artifacts'] == artifacts(),
            'production build/source provenance mismatch')
    symbols = symbols_from_map(map_path)
    users = symbols_from_map(image.parent/'recovery-user/files.map')
    policy = symbols_from_map(image.parent/'recovery-user/policy.map')
    size, fields = task_layout()
    hashes = {name: digest(path) for name,path in dict(image=image,map=map_path,emulator=emulator,rom=rom).items()}
    with ready_monitor(image.read_bytes(),emulator,rom,timeout,full_image=True) as opened:
        monitor, process, stdout, stderr = opened
        transcript = [monitor.receive(),monitor.stop_at(symbols['taskStart'])]
        monitor.command(f"del 0x{symbols['taskStart']:X}")
        if window is not None:
            directory = monitor.words(symbols['tasks'] + fields['directory'], 1)[0]
            for name, value in zip(('policy__recoveryDiskOffset', 'policy__recoveryDiskBytes'), window):
                va = policy[name]
                table = monitor.words(directory + (va >> 22) * 4, 1)[0] & ~4095
                physical = (monitor.words(table + ((va >> 12) & 1023) * 4, 1)[0] & ~4095) + (va & 4095)
                transcript.append(monitor.command(f'wp 0x{physical:X} {value}'))
            configured = monitor.registers(monitor.stop_at(symbols['deviceExtentConfigure']))
            require([configured['r2'], configured['r3']] == list(window), 'user manager did not select its window')
            monitor.command(f"del 0x{symbols['deviceExtentConfigure']:X}")
        transcript.append(monitor.stop_at(users['fileHandle']))
        transcript.append(monitor.stop_at(users['fileHandle']))
        # xp is physical; translate the Files global through its own directory.
        va = users['server__response']
        directory = monitor.words(symbols['tasks']+3*size+fields['directory'],1)[0]
        table = monitor.words(directory+(va>>22)*4,1)[0] & ~4095
        physical = (monitor.words(table+((va>>12)&1023)*4,1)[0] & ~4095)+(va&4095)
        response = monitor.words(physical,8)
        require(response[:4] == [0x001C0201,0,1,16], f'production Files response: {response}')
        if window is not None:
            import struct
            image_bytes = image.read_bytes()
            first = struct.unpack_from('<I', image_bytes, 4)[0] * 512 + window[0]
            require(struct.pack('<4I', *response[4:]) == image_bytes[first:first + 16],
                    'CPU read escaped the user-configured extent')
            extent = next(module.scope['DeviceExtent'].type for module in check_m(LAIX/'src/drivers/service_devices.m')
                          if 'DeviceExtent' in module.scope and module.scope['DeviceExtent'].type)
            require(monitor.words(symbols['service_devices__deviceExtent'] + extent.field('bytes').offset, 1) == [window[1]],
                    'approved user window length lost')

        for slot in range(1,6):
            state = monitor.words(symbols['tasks']+(slot-1)*size+fields['state'],1)[0]
            require(state in (1,2,4),f'production task {slot} died: {state}')
        require(monitor.words(symbols['taskHistoryCount'],1) == [0], 'production task died')
        uart = uart_text(stdout)
        require('PANIC' not in uart, 'production panic')
    require(source_manifest() == build_record['sources'] and artifacts() == build_record['artifacts'],
            'production provenance changed during CPU run')
    return dict(complete=True,production=True,successful_read=True,live_tasks=5,
                window=window,sha256=hashes,build=build_record),uart,'\n'.join(transcript)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image',type=Path)
    parser.add_argument('map',type=Path)
    parser.add_argument('--emulator',type=Path,default=ROOT/'bin/wrm081632')
    parser.add_argument('--rom',type=Path,default=ROOT/'bin/firmware.rom')
    parser.add_argument('--production',action='store_true')
    parser.add_argument('--window', type=int, nargs=2, metavar=('OFFSET', 'BYTES'), help='user manager subextent for production acceptance')
    parser.add_argument('--timeout',type=float,default=60)
    parser.add_argument('--log-dir',type=Path,default=LAIX/'build/acceptance/service-recovery')
    args=parser.parse_args();args.log_dir.mkdir(parents=True,exist_ok=True)
    try:
        run = production_probe if args.production else probe
        report,uart,transcript=run(args.image,args.map,args.emulator.resolve(),args.rom.resolve(),args.timeout,
                                  **(dict(window=args.window) if args.production else {}))
        (args.log_dir/('production.uart.txt' if args.production else 'recovery.uart.txt')).write_text(uart)
        (args.log_dir/('production.monitor.txt' if args.production else 'recovery.monitor.txt')).write_text(transcript)
    except (ValueError,OSError,subprocess.TimeoutExpired) as error:
        report=dict(complete=False,error=str(error))
    (args.log_dir/('production.json' if args.production else 'results.json')).write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    print(('PASS' if report['complete'] else 'FAIL')+' service recovery CPU: '+ (('production read and five live tasks' if args.production else 'five generations, eight faults and explicit reconnects') if report['complete'] else report['error']))
    return 0 if report['complete'] else 1
if __name__=='__main__':
    raise SystemExit(main())

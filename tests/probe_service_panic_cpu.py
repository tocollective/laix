#!/usr/bin/env python3
"""Full UART panic dumps with blocked/dead screen services on ready images.

Only a saved supervisor return frame is redirected to an existing BREAK.
No instructions, mappings, emulator or firmware are built or modified.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from probe_boot import ready_monitor, require
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import uart_text
from probe_screen_cpu import ScreenProbe
from probe_unexpected_traps import check_panic, locations
from run_ready import ROOT
from source_m import LAYOUT as C


def run_case(case, image, symbols, services, emulator, rom, timeout, log_dir):
    traps, dispatch_return = locations(image, symbols)
    with ready_monitor(image, emulator, rom, timeout, full_image=True,
                       extra_args=('--ram', '2M')) as opened:
        monitor, process, stdout, stderr = opened
        probe = ScreenProbe(monitor, symbols, services)
        probe.prepare()
        if case == 'dead':
            probe.fault(1, 0, fetch=True)
            require(probe.field(1, 'state') == 3 and probe.field(1, 'reaped') == 1,
                    'screen service did not die and release its resources')
        else:
            probe.finish()
            require(probe.field(3, 'exitCode') == 0, 'normal output failed')
            require(probe.field(1, 'state') == 4 and probe.field(1, 'waitReason') == 5,
                    'screen service is not blocked in accept')
        require(probe.field(2, 'state') == 4 and probe.field(2, 'waitReason') == 5,
                'storage service is not blocked in accept')

        # Wait for a real timer trap from supervisor idle. Modify its selected
        # return frame only, then let IRET and the CPU raise the fatal BREAK.
        probe.log.append(monitor.command('del all'))
        timer = probe.stop(symbols['trapEntry'])
        require(timer['cause'] == 0 and timer['status'] & C['STATUS_PUM'] == 0,
                'expected a timer interrupt from supervisor idle')
        returned = probe.stop(dispatch_return)
        frame = returned['r1']
        require(monitor.words(frame + C['TF_PTBR'], 1)[0] == timer['ptbr'],
                'timer returned a different address space')
        epc = traps[C['CAUSE_BREAKPOINT']]
        probe.log.append(monitor.commands([
            f'wp 0x{frame + C["TF_EPC"]:X} 0x{epc:X}',
            f'wp 0x{frame + C["TF_STATUS"]:X} 0x{C["STATUS_EXL"]:X}',
        ]))
        probe.log.append(monitor.command('del all'))
        before = probe.stop(symbols['trapEntry'])
        require(before['cause'] == C['CAUSE_BREAKPOINT'] and before['epc'] == epc,
                'CPU did not raise the redirected supervisor BREAK')
        require(before['status'] == C['STATUS_EXL'], 'fatal trap has the wrong origin')
        probe.stop(symbols['panic'])
        screen_state = probe.field(1, 'state')
        require(screen_state == (3 if case == 'dead' else 4),
                'panic depends on waking the screen service')
        probe.log.append(monitor.command('del all'))
        monitor.connection.sendall(b'c\n')
        process.wait(timeout=timeout)
        uart = uart_text(stdout)
        stderr.seek(0)
        emulator_output = stderr.read()
        (log_dir / f'{case}.uart.txt').write_text(uart)
        (log_dir / f'{case}.emulator.txt').write_text(emulator_output)
        (log_dir / f'{case}.monitor.txt').write_text('\n'.join(probe.log))
        require(uart.count('LA/IX PANIC:') == 1, 'missing or repeated panic')
        # Earlier task-death diagnostics also contain EPC; validate only the
        # complete fatal dump while retaining the entire UART transcript.
        dump = uart[uart.index('LA/IX PANIC:'):]
        check_panic(dump, process.returncode, C['CAUSE_BREAKPOINT'], False, epc, before)
        require('stage=user-task' in dump, 'missing boot stage in panic dump')
        require('double fault' not in emulator_output.lower(), 'panic raised a double fault')
        return {'case': case, 'exit_code': process.returncode, 'epc': f'{epc:08X}',
                'gprs_checked': 32, 'screen_state': screen_state}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--services', type=Path, default=ROOT / 'laix/build/services')
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=ROOT / 'laix/build/acceptance/service-panic')
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('timeout must be positive')
    paths = {'image': args.image, 'map': args.map, 'emulator': args.emulator, 'rom': args.rom}
    paths.update({name: args.services / (name + '.elf') for name in ('screen', 'storage', 'application')})
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    args.log_dir.mkdir(parents=True, exist_ok=True)
    report = {'complete': False, 'cases': [], 'sha256': hashes, 'ram': '2M'}
    try:
        image, symbols = args.image.read_bytes(), symbols_from_map(args.map)
        for case in ('blocked', 'dead'):
            report['cases'].append(run_case(case, image, symbols, args.services,
                                           args.emulator.resolve(), args.rom.resolve(), args.timeout, args.log_dir))
            print('PASS service panic CPU: ' + case, flush=True)
        require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name]
                    for name, path in paths.items()), 'input artifact changed')
        report['complete'] = True
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        report['error'] = str(exc)
        print('FAIL service panic CPU: ' + str(exc), flush=True)
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

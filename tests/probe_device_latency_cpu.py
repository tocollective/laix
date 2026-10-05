#!/usr/bin/env python3
"""Measure maximum broker copying and real DMA/IRQ progress on existing WRM.

The first real Disk submission is enlarged to one approved sector through its
saved syscall frame. Finish copies into a valid private user page with two
canaries. Only contexts/data are edited; no MMIO, PTE or instruction patches.
"""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import struct

from probe_boot import ready_monitor, require, Monitor
from probe_mmu_cpu import symbols_from_map
from probe_simple_services_cpu import SimpleProbe
from probe_scheduler_cpu import uart_text
from probe_limits_latency_cpu import counters, iret_pc, summarize, CLOCK, SECTION_BUDGET
from source_m import LAYOUT as C
from run_ready import ROOT


def run(args):
    paths = dict(image=args.image, map=args.map, emulator=args.emulator, rom=args.rom)
    paths.update({name: args.services / (name + '.elf') for name in ('input', 'disk', 'files', 'simple-application')})
    hashes = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    data, symbols = args.image.read_bytes(), symbols_from_map(args.map)
    samples, input_max, submitted, irqs, timers = [], False, False, 0, 0
    with ready_monitor(data, args.emulator, args.rom, args.timeout, full_image=True,
                       extra_args=('--ram', '2M', '--clock', '128M')) as opened:
        m, process, stdout, stderr = opened
        p = SimpleProbe(m, symbols, args.services)
        try:
            p.prepare()
            iret = iret_pc(data, symbols)
            destination = p.records[1][8] + 128
            root = p.field(2, 'directory')
            physical = (p.leaf(root, destination) & ~4095) + (destination & 4095)
            p.log.append(m.commands([f'wp 0x{physical - 4:X} 0xC0FFEE', f'wp 0x{physical + 512:X} 0xC0FFEE']))
            for _ in range(2000):
                m.command('del all')
                entry = p.stop(symbols['trapEntry'])
                start = counters(m.command('r'))
                require(entry['status'] & 16, 'kernel entry lacks EXL')
                if entry['cause'] == 0:
                    irqs += 1
                    # PIC CLAIM is observational and does not acknowledge.
                    info = m.command('info')
                    if 'expired' in next(line for line in info.splitlines() if line.startswith('timer')):
                        timers += 1
                    label = 'device_or_timer_irq'
                else:
                    require(entry['cause'] == 12, 'unexpected user fault')
                    number = entry['r9']
                    label = f'syscall_{number}'
                    if number in (C['SYS_INPUT_READ'], C['SYS_DEVICE_SUBMIT'], C['SYS_DEVICE_FINISH']):
                        m.command('del all')
                        dispatch = p.stop(symbols['trap__userSyscall'])
                        frame = dispatch['r1']
                        if number == C['SYS_INPUT_READ']:
                            p.log.append(m.command(f'wp 0x{frame + C["TF_R2"]:X} 32'))
                            input_max = True
                        elif number == C['SYS_DEVICE_SUBMIT']:
                            require(not submitted and entry['r1'] == 0, 'unexpected initial disk offset')
                            p.log.append(m.command(f'wp 0x{frame + C["TF_R2"]:X} 512'))
                            submitted = True
                            submitted_at = start[0]
                        else:
                            require(submitted, 'finish before submission')
                            p.log.append(m.command(f'wp 0x{frame + C["TF_R2"]:X} 0x{destination:X}'))
                m.command('del all')
                returned = p.stop(iret)
                end = counters(m.command('r'))
                samples.append(dict(kind=label, cycles=end[0] - start[0], retired=end[1] - start[1]))
                if entry['cause'] == 12 and entry['r9'] == C['SYS_INPUT_READ']:
                    require(returned['r1'] == 136, 'maximum Input batch failed')
                if entry['cause'] == 12 and entry['r9'] == C['SYS_DEVICE_FINISH']:
                    require(returned['r1'] == 512, 'maximum device finish failed')
                    actual = struct.pack('<128I', *m.words(physical, 128))
                    sector = struct.unpack_from('<I', data, 4)[0] * 512
                    require(actual == data[sector:sector + 512], 'maximum DMA payload differs from disk')
                    require(m.words(physical - 4, 1) == [0xC0FFEE] and m.words(physical + 512, 1) == [0xC0FFEE], 'copy crossed canaries')
                    require(m.words(symbols['service_devices__deviceBounce'], 1) == [0], 'DMA buffer leaked')
                    require(input_max and irqs > 0, 'Input/device IRQ progress was not observed')
                    # A complete submitted request must finish within its
                    # five-second IRQ wait plus the kernel section budget.
                    elapsed = end[0] - submitted_at
                    require(elapsed <= CLOCK * 5 + SECTION_BUDGET, 'device progress exceeded declared deadline')
                    summary = summarize(samples)
                    require(max(row['maximum_cycles'] for row in summary.values()) <= SECTION_BUDGET, 'device section exceeds budget')
                    report = dict(complete=True, clock_hz=CLOCK, ram_bytes=2 * 1024 * 1024,
                                  input_bytes=136, disk_bytes=512, irq_entries=irqs, timer_entries=timers,
                                  device_progress_cycles=elapsed, device_progress_budget_cycles=CLOCK * 5 + SECTION_BUDGET,
                                  canaries_preserved=True, dma_pin_released=True, measurements=summary, sha256=hashes)
                    break
            else:
                raise ValueError('device did not finish within bounded trap count')
        finally:
            (args.log_dir / 'device.monitor.txt').write_text('\n'.join(p.log))
            (args.log_dir / 'device.uart.txt').write_text(uart_text(stdout))
    require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[key] for key, path in paths.items()), 'device probe inputs changed')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--services', type=Path, default=ROOT / 'laix/build/services')
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'laix/build/acceptance/screen-firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=ROOT / 'laix/build/acceptance/limits-latency-device')
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = run(args)
    except (ValueError, OSError, socket.timeout) as error:
        report = dict(complete=False, error=str(error))
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' device latency CPU: ' + str(report.get('measurements', report.get('error'))))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Real host-script HID arrival/overflow, broker copying and concurrent clients."""
import argparse
from collections import Counter, deque
import hashlib
import json
from pathlib import Path
import re
import socket
import tempfile

from probe_boot import ready_monitor, Monitor, require
from probe_simple_services_cpu import SimpleProbe
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import uart_text, SchedulerProbe
from run_ready import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    paths = dict(image=args.image, map=args.map, emulator=args.emulator, rom=args.rom)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    report = dict(complete=False, sha256=hashes)
    try:
        data, symbols = args.image.read_bytes(), symbols_from_map(args.map)
        config = ('--ram', '8M', '--clock', '128M', '--deterministic')
        # Calibrate against the same existing image/config, before host input.
        with ready_monitor(data, args.emulator.resolve(), args.rom.resolve(), args.timeout,
                           full_image=True, extra_args=config) as opened:
            m, process, stdout, stderr = opened
            m.receive()
            m.stop_at(symbols['taskStart'])
            tick = SchedulerProbe.parse_devices(m.command('info'))['count'] + 1
        script = '\n'.join(f'{tick} key {4 + i // 2} {"up" if i % 2 else "down"}' for i in range(64)) + '\n'
        (args.log_dir / 'host.input').write_text(script)
        events, flags, calls, replies, pending, timers = [], [], Counter(), Counter(), deque(), 0
        with ready_monitor(data, args.emulator.resolve(), args.rom.resolve(), args.timeout,
                           full_image=True, extra_args=(*config, '--input', str((args.log_dir / 'host.input').resolve()))) as opened:
            m, process, stdout, stderr = opened
            m.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            p = SimpleProbe(m, symbols, ROOT / 'laix/build/services')
            try:
                p.prepare()
                require(SchedulerProbe.parse_devices(m.command('info'))['count'] == tick - 1,
                        'host-input calibration differs from actual run')
                require(all(p.field(slot, 'state') == 1 for slot in range(4, 9)), 'concurrent Input clients absent')
                tokens = {slot: m.words(p.field(slot, 'bootPage') + 48, 1)[0] for slot in range(4, 9)}
                m.command('del all')
                locations = {symbols[name]: name for name in ('ipcCall', 'ipcReply', 'inputRead', 'taskFinish', 'taskTick')}
                for pc in locations:
                    m.command(f'b 0x{pc:X}')
                hardware_full = False
                finished = set()
                for _ in range(20000):
                    m.command('c')
                    raw = m.command('r')
                    regs = Monitor.registers(raw)
                    name = locations.get(regs['pc'])
                    require(name is not None, 'unexpected Input CPU stop')
                    current = m.words(symbols['currentTask'], 1)[0]
                    slot = m.words(current, 1)[0]
                    if name == 'taskTick':
                        timers += 1
                        continue
                    p.log.append(raw)
                    if name == 'inputRead' and not hardware_full:
                        info = m.command('info')
                        p.log.append(info)
                        require('keys    32 events' in info, 'host events did not fill real FIFO')
                        hardware_full = True
                    elif name == 'ipcCall' and slot in tokens and regs['r2'] == tokens[slot]:
                        calls[slot] += 1
                        pending.append(slot)
                    elif name == 'ipcReply' and slot == 1:
                        client = regs['r2'] & 255
                        require(pending and pending.popleft() == client, 'Input reordered concurrent callers')
                        va = regs['r3']
                        pa = (p.leaf(p.field(1, 'directory'), va) & ~4095) + (va & 4095)
                        response = m.words(pa, 8)
                        require(regs['r4'] == 32 and response[1] == 0 and response[2] <= 4, 'Input reply failed')
                        replies[client] += 1
                        flags.append(response[3])
                        events.extend(response[4:4 + response[2]])
                    elif name == 'taskFinish' and slot in tokens:
                        require(regs['r2'] == regs['r3'] == 0, 'compiled Input client failed')
                        finished.add(slot)
                    if finished == set(tokens):
                        break
                else:
                    raise ValueError('Input campaign did not finish')
                expected = [(4 + i // 2) | (0x80000000 if i % 2 else 0) for i in range(32)]
                require(events == expected, 'HID events were lost, duplicated or reordered across clients')
                require(sum(flags) == 1 and flags[0] == 1, 'physical overflow was not reported exactly once')
                require(calls == replies == Counter({slot: 8 for slot in tokens}) and not pending,
                        'Input clients lost completion')
                require(timers >= 800, 'concurrent Input lacks sustained hardware timer progress')
                p.only(symbols['taskKernelResume.idle'])
                require(all(p.field(slot, 'reaped') == 1 for slot in tokens), 'Input client resources retained')
                require(p.field(1, 'state') == 4 and p.field(1, 'waitReason') == 5, 'Input failed to return to accept')
                require('PANIC' not in uart_text(stdout), 'HID kernel panic')
                report.update(complete=True, host_script_sha256=hashlib.sha256(script.encode()).hexdigest(),
                              arrival_tick=tick, submitted_events=64, delivered_events=events,
                              dropped_events=32, overflow_once=True, clients=list(tokens),
                              requests=dict(calls), replies=dict(replies), timer_irqs=timers,
                              method='--input -> keyboard_key -> real FIFO/MMIO -> compiled Input -> client helper')
            finally:
                (args.log_dir / 'hid.monitor.txt').write_text('\n'.join(p.log))
                (args.log_dir / 'hid.uart.txt').write_text(uart_text(stdout))
                stderr.seek(0)
                (args.log_dir / 'hid.emulator.txt').write_text(stderr.read())
        require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()), 'HID inputs changed')
    except (ValueError, OSError, KeyError, StopIteration) as error:
        report['error'] = str(error)
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' HID CPU: ' + str(report), flush=True)
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

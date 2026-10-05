#!/usr/bin/env python3
"""FIFO output and sustained timer progress with compiled M application images."""
import argparse
from collections import Counter, deque
import hashlib
import json
import re
from pathlib import Path
import socket

from probe_boot import ready_monitor, require, Monitor
from probe_screen_cpu import ScreenProbe
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import uart_text
from run_ready import ROOT


def run(args):
    paths = dict(image=args.image, map=args.map, emulator=args.emulator, rom=args.rom,
                 client=ROOT / 'laix/build/services/stress-client.elf',
                 client_map=ROOT / 'laix/build/services/stress-client.map')
    hashes = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    symbols = symbols_from_map(args.map)
    clients = tuple(range(3, 7)) if args.profile == 'uart' else tuple(range(4, 8))
    initial = 2 if args.profile == 'uart' else 3
    pending, requests, replies, timers = deque(), Counter(), Counter(), 0
    output = bytearray()
    finished = set()
    with ready_monitor(args.image.read_bytes(), args.emulator.resolve(), args.rom.resolve(), args.timeout,
                       full_image=True, extra_args=('--ram', '8M', '--clock', '128M', '--deterministic')) as opened:
        monitor, process, stdout, stderr = opened
        monitor.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        p = ScreenProbe(monitor, symbols, ROOT / 'laix/build/services')
        try:
            monitor.receive()
            p.stop(symbols['taskStart'])
            require(all(p.field(slot, 'state') == 1 for slot in clients), 'compiled clients not published')
            tokens = {slot: monitor.words(p.field(slot, 'bootPage') + 20, 1)[0] for slot in (*clients, initial)}
            def user_bytes(slot, va, count):
                root = p.field(slot, 'directory')
                data = bytearray()
                for offset in range(count):
                    address = va + offset
                    pa = (p.leaf(root, address) & ~4095) + (address & 4095)
                    word = monitor.words(pa & ~3, 1)[0]
                    data.append((word >> ((pa & 3) * 8)) & 255)
                return bytes(data)
            monitor.command('del all')
            locations = {symbols[name]: name for name in ('ipcCall', 'ipcReply', 'taskFinish', 'taskTick')}
            for pc in locations:
                monitor.command(f'b 0x{pc:X}')
            # Four clients each execute 128 one-second timer sleeps. Include
            # 100 Hz IRQs, ordinary boot output and nested bitmap IPC.
            for _ in range(100000):
                monitor.command('c')
                raw = monitor.command('r')
                regs = Monitor.registers(raw)
                name = locations.get(regs['pc'])
                require(name is not None, 'CPU stopped outside acceptance breakpoints')
                current = monitor.words(symbols['currentTask'], 1)[0]
                slot = monitor.words(current, 1)[0]
                if name == 'taskTick':
                    require(regs['cause'] == 0 and regs['status'] & 16, 'timer did not enter through real IRQ')
                    timers += 1
                    if timers % 1000 == 0:
                        print(f'  stress: {timers} hardware timer IRQs', flush=True)
                    continue
                p.log.append(raw)
                if name == 'ipcCall' and slot in tokens and regs['r2'] == tokens[slot]:
                    payload = user_bytes(slot, regs['r3'], regs['r4'])
                    require(len(payload) >= 4, 'short console message')
                    text = payload[4:]
                    if slot in clients:
                        require(text == bytes((65 + slot, 10)), 'client output identity corrupted')
                        requests[slot] += 1
                    pending.append((slot, text))
                elif name == 'ipcReply' and slot == 1:
                    client = regs['r2'] & 255
                    require(pending and pending[0][0] == client, 'service reordered concurrent clients')
                    target, text = pending.popleft()
                    response = user_bytes(1, regs['r3'], regs['r4'])
                    require(len(response) == 12 and int.from_bytes(response[4:8], 'little') == 0 and
                            int.from_bytes(response[8:12], 'little') == len(text), 'output reply failed or partial')
                    output.extend(text)
                    if client in clients:
                        replies[client] += 1
                elif name == 'taskFinish' and slot in clients:
                    require(regs['r2'] == 0 and regs['r3'] == 0, 'compiled stress client failed')
                    finished.add(slot)
                if finished == set(clients):
                    break
            else:
                raise ValueError('sustained workload did not finish within bounded CPU events')
            monitor.command('del all')
            p.stop(symbols['taskKernelResume.idle'])
            require(requests == replies == Counter({slot: 128 for slot in clients}) and not pending,
                    'missing or duplicate application completions')
            require(timers >= 12800, 'one-second sleeps did not sustain 128 seconds of timer progress')
            for slot in clients:
                require(p.field(slot, 'reaped') == 1 and p.field(slot, 'directory') == 0 and
                        p.field(slot, 'kernelStackTop') == 0, 'stress client retained runtime resources')
            require(p.field(1, 'state') == 4 and p.field(1, 'waitReason') == 5, 'output server failed to sleep')
            if args.profile == 'screen':
                require(p.field(2, 'state') == 4 and p.field(2, 'waitReason') == 5, 'bitmap server stopped progressing')
                require(monitor.words(symbols['service_devices__deviceBounce'], 1) == [0], 'DMA pin leaked')
            uart = uart_text(stdout)
            require('PANIC' not in uart, 'multiclient kernel panic')
            if args.profile == 'uart':
                service_uart = re.sub(r'LA/IX: task [0-9]+ stopped, state=[0-9]+ code=-?[0-9]+ cause=[0-9]+ epc=[0-9A-Fa-f]+\n', '', uart)
                require(bytes(output).decode('ascii') in service_uart, 'UART bytes differ from FIFO reply order')
        finally:
            (args.log_dir / 'stress.monitor.txt').write_text('\n'.join(p.log))
            (args.log_dir / 'stress.uart.txt').write_text(uart_text(stdout))
            stderr.seek(0)
            (args.log_dir / 'stress.emulator.txt').write_text(stderr.read())
    require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[key] for key, path in paths.items()), 'stress inputs changed')
    return dict(complete=True, profile=args.profile, sha256=hashes, clients=list(clients), rounds=128,
                requests=dict(requests), replies=dict(replies), timer_irqs=timers, fifo=True,
                compiled_helpers=True, reaped=True, virtual_seconds_at_least=128)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--profile', choices=('uart', 'screen'), required=True)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--log-dir', type=Path, required=True)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = run(args)
    except (ValueError, OSError, KeyError, StopIteration) as error:
        report = dict(complete=False, error=str(error))
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' multiclient CPU: ' + str(report), flush=True)
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

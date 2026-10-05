#!/usr/bin/env python3
"""Measure executed IRQ-excluded CPU work; no WRM build or instruction patches.

Counters come from the paused emulator's cumulative CPU cycles and retired
instructions. Monitor pause/inspection time contributes zero virtual ticks.
Saved contexts and trusted data fixtures use existing kernel/user instructions.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess

from probe_boot import ready_monitor, require, disassemble
from probe_ipc_liveness_cpu import LivenessProbe, TIMED, CANCEL
from probe_ipc_request_reply_cpu import preflight, negative
from probe_mmu_cpu import symbols_from_map
from probe_unexpected_traps import instruction
from probe_scheduler_cpu import DATA, C, uart_text
from run_ready import ROOT

# Acceptance envelope: 128 MHz, at most 32 MiB, bounded pools as published.
CLOCK = 128_000_000
SECTION_BUDGET = 64_000_000  # 500 ms; not a real-time scheduling promise.


def counters(text):
    match = re.search(r'cycles (\d+)\s+retired (\d+)', text)
    require(match is not None, 'monitor lacks cumulative CPU counters')
    return tuple(map(int, match.groups()))


def iret_pc(data, symbols):
    points = [pc for pc in range(symbols['trapEntry.restore'], symbols['trapEntry.bad_stack'], 4)
              if disassemble(instruction(data, pc), pc) == 'iret']
    require(len(points) == 1, 'restore path lacks a unique IRET')
    return points[0]


class LatencyProbe(LivenessProbe):
    def __init__(self, *args):
        self.samples = []
        self.pending_measurement = None
        self.measuring = False
        self.last_timer = None
        self.timer_gaps = []
        super().__init__(*args)
        self.iret = iret_pc(self.data, self.s)

    def start_service(self):
        super().start_service(count=8, deferred=8)

    def stop(self, pc):
        if self.measuring and self.pending_measurement and pc in (self.busy, self.syscalls[-1]):
            regs = super().stop(self.iret)
            require(regs['status'] & 16 != 0, 'timed kernel return lost EXL')
            end = counters(self.cmd('r'))
            label, start = self.pending_measurement
            self.samples.append(dict(kind=label, cycles=end[0] - start[0], retired=end[1] - start[1]))
            self.pending_measurement = None
        regs = super().stop(pc)
        if self.measuring and pc == self.s['trapEntry']:
            require(self.pending_measurement is None, 'nested measured trap')
            label = 'timer_irq' if regs['cause'] == 0 else f'syscall_{regs["r9"]}'
            point = counters(self.cmd('r'))
            if regs['cause'] == 0:
                if self.last_timer is not None:
                    self.timer_gaps.append(point[0] - self.last_timer)
                self.last_timer = point[0]
            self.pending_measurement = label, point
        elif pc == self.s['trapEntry'] and regs['cause'] == 0:
            self.last_timer = counters(self.cmd('r'))[0]
        return regs

    def kernel_api(self, name, *args):
        # Measure just the injected helper, independently of its preceding IRQ.
        active = self.measuring
        self.last_timer = None
        self.measuring = False
        self.pending_measurement = None
        try:
            returned = self.tick_return()
            slot = self.running()
            original = self.context(slot)
            self.inject_call(slot, returned, name, args)
            self.stop(self.s[name])
            start = counters(self.cmd('r'))
            restored = self.stop(self.s['trapEntry.restore'])
            end = counters(self.cmd('r'))
            result = restored['r1']
            if active and name != 'latencyFillTasks':
                self.samples.append(dict(kind=name, cycles=end[0] - start[0], retired=end[1] - start[1]))
            self.stop(self.busy)
            self.m.commands([f'wp 0x{self.address(slot, "context") + i * 4:X} 0x{word:X}'
                             for i, word in enumerate(original)])
            self.check_queue(self.running())
            self.check_pending_contexts()
            return result
        finally:
            self.measuring = active

    def memory_workload(self):
        # Self-owned 16-page source and a supervised unpublished destination.
        child = 8  # bootstrap-owned Created child with scoped loader authority
        cap = self.syscall(6, C['SYS_MEM_SPACE'], 0, 15)[0]
        source = self.syscall(6, C['SYS_MEM_ALLOC'], cap, 16)[0]
        require(0 < source < 0x80000000, 'maximum source allocation failed')
        va = C['MEM_VA_START'] + 0x3FF000
        require(self.syscall(6, C['SYS_MEM_MAP'], cap, source, va, 0, 16, 23)[0] == 0, 'source mapping failed')
        loader = self.syscall(6, C['SYS_MEM_SPACE'], child, C['MEM_RIGHT_ALL'])[0]
        region = self.syscall(6, C['SYS_MEM_ALLOC'], loader, 16)[0]
        require(0 < region < 0x80000000, 'maximum destination allocation failed')
        require(self.syscall(6, C['SYS_MEM_POPULATE'], loader, region, 0, va, 65536)[0] == 0, '64 KiB populate failed')
        require(self.syscall(6, C['SYS_MEM_POPULATE'], loader, region, 0, va, 65537)[0] == negative(22), 'oversized populate admitted')
        require(self.syscall(6, C['SYS_MEM_UNMAP'], cap, va, 16)[0] == 0, 'maximum unmap failed')
        require(self.syscall(6, C['SYS_MEM_RELEASE'], cap, source)[0] == 0, 'source release failed')
        require(self.syscall(6, C['SYS_TASK_PUBLISH'], child)[0] == 0, 'child publication failed')
        # Restore the boot self-branch in the replacement fixture's saved frame.
        self.seed(8, self.busy)
        return dict(allocation_pages=16, populate_bytes=65536, edited_pages=16, child_reference=child)

    def finish_measurement(self):
        regs = self.stop(self.iret)
        end = counters(self.cmd('r'))
        require(regs['status'] & 16, 'measured return escaped EXL')
        label, start = self.pending_measurement
        self.samples.append(dict(kind=label, cycles=end[0] - start[0], retired=end[1] - start[1]))
        self.pending_measurement = None
        return regs

    def cleanup_all(self):
        require(self.kernel_api('latencyFillTasks', 1) == 8, 'did not fill all eight budgets')
        # Retire seven peers without reaping, then immediately issue the final
        # client's real EXIT. Its trap switches to idle and reaps all eight.
        self.measuring = False
        self.pending_measurement = None
        returned = self.tick_return()
        slot = self.running()
        self.inject_call(slot, returned, 'latencyRetirePeers', ())
        self.stop(self.s['latencyRetirePeers'])
        restored = self.stop(self.s['trapEntry.restore'])
        require(restored['r1'] == 7, 'did not retire seven peers')
        self.set_frame(restored['r30'], dict(EPC=self.syscalls[-1], R9=C['SYS_EXIT'], R1=0))
        self.stop(self.syscalls[-1])
        self.measuring = True
        trapped = self.stop(self.s['trapEntry'])
        require(trapped['cause'] == 12 and trapped['r9'] == C['SYS_EXIT'], 'final EXIT was not executed')
        require(self.last_timer is not None, 'cleanup lacks preceding timer entry')
        self.finish_measurement()
        for slot in range(1, 9):
            require(self.field(slot, 'directory') == 0 and self.field(slot, 'kernelStackBottom') == 0,
                    'bulk reaping retained physical resources')
        # Idle WFI must subsequently deliver a real timer interrupt.
        self.stop(self.s['taskKernelResume.idle'])
        trapped = self.stop(self.s['trapEntry'])
        require(trapped['cause'] == 0, 'idle timer did not progress after cleanup')
        self.finish_measurement()

    def fair_server(self, rounds):
        for _ in range(rounds):
            for slot in range(2, 9):
                self.put_bytes(slot, 0, bytes([slot]) * 32)
                self.syscall(slot, TIMED, self.tokens[slot], DATA, 32, DATA + 128, 32, 60)
            held = self.accept()
            require(held & 255 == 2, 'first caller lost FIFO order')
            for slot in range(3, 9):
                token = self.accept()
                require(token & 255 == slot, 'caller bypassed FIFO')
                self.reply(token, bytes([slot]) * 32)
                require(self.result(slot) == (32, 32), 'peer failed behind held client')
            # A held/abandoned client consumes only its own one wait record.
            require(self.syscall(6, CANCEL, 2)[0] == 0, 'watchdog cancellation failed')
            require(self.reply(held)[0] == negative(9), 'cancelled reply retained authority')
            require(self.endpoint_field('references') == 8, 'fairness stress leaked a pin')
        return dict(rounds=rounds, callers=7, fifo=True, held_client_cancelled=True)


def summarize(samples):
    result = {}
    for sample in samples:
        row = result.setdefault(sample['kind'], dict(samples=0, maximum_cycles=0, maximum_retired=0))
        row['samples'] += 1
        row['maximum_cycles'] = max(row['maximum_cycles'], sample['cycles'])
        row['maximum_retired'] = max(row['maximum_retired'], sample['retired'])
    for row in result.values():
        row['maximum_ms_at_128MHz'] = round(row['maximum_cycles'] * 1000 / CLOCK, 6)
    return result


def run(args):
    paths = dict(image=args.image, map=args.map, emulator=args.emulator, rom=args.rom)
    hashes = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    data, symbols = args.image.read_bytes(), symbols_from_map(args.map)
    layout = preflight(data, symbols)
    with ready_monitor(data, args.emulator, args.rom, args.timeout, extra_args=('--ram', '32M', '--clock', '128M')) as opened:
        monitor, process, stdout, stderr = opened
        p = LatencyProbe(monitor, data, symbols, args.timeout, layout)
        try:
            p.start_service()
            p.measuring = True
            memory = p.memory_workload()
            fairness = p.fair_server(args.rounds)
            p.write(p.address(2, 'ipcCallGeneration'), 0x7FFFFE)
            p.syscall(2, TIMED, p.tokens[2], DATA, 32, DATA + 128, 32, 60)
            last = p.accept()
            require(last == 0x7FFFFF02, 'wrong last-valid reply identity')
            p.reply(last)
            require(p.syscall(2, TIMED, p.tokens[2], DATA, 32, DATA + 128, 32, 60) == (negative(75), 0), 'generation wrapped')
            require(p.reply(last)[0] == negative(9), 'used final identity regained authority')
            p.cleanup_all()
            summary = summarize(p.samples)
            maximum = max(row['maximum_cycles'] for row in summary.values())
            require(maximum <= SECTION_BUDGET, 'IRQ-disabled section exceeds acceptance budget')
            require(max(p.timer_gaps) <= SECTION_BUDGET + CLOCK // 100, 'timer progress exceeded budget')
            uart = uart_text(stdout)
            require('PANIC' not in uart, 'latency image panicked')
            report = dict(complete=True, measurement='executed CPU counters; trapEntry to IRET, including reaping',
                          clock_hz=CLOCK, ram_bytes=32 * 1024 * 1024, section_budget_cycles=SECTION_BUDGET,
                          fairness=fairness, memory=memory, simultaneous_reaped_tasks=8,
                          per_peer_frames=96, per_peer_tables=8, per_peer_mappings=128,
                          timer_progress_after_cleanup=True, generation_boundary=True,
                          maximum_timer_gap_cycles=max(p.timer_gaps),
                          timer_progress_budget_cycles=SECTION_BUDGET + CLOCK // 100,
                          measurements=summary, sha256=hashes)
        finally:
            (args.log_dir / 'cpu.monitor.txt').write_text('\n'.join(p.log))
            (args.log_dir / 'cpu.uart.txt').write_text(uart_text(stdout))
    require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[key] for key, path in paths.items()), 'inputs changed during probe')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'laix/build/acceptance/screen-firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=ROOT / 'laix/build/acceptance/limits-latency')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--rounds', type=int, default=4)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = run(args)
    except (ValueError, OSError, socket.timeout, subprocess.TimeoutExpired) as error:
        report = dict(complete=False, error=str(error))
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' limits/latency CPU: ' + (str(report.get('measurements', '')) if report['complete'] else report['error']))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

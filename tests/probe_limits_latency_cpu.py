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

from probe_boot import Monitor, ready_monitor, require, disassemble
from probe_ipc_liveness_cpu import LivenessProbe, TIMED, CANCEL
from probe_ipc_request_reply_cpu import preflight, negative
from probe_mmu_cpu import symbols_from_map
from probe_unexpected_traps import instruction
from probe_scheduler_cpu import DATA, C, uart_text
from run_ready import ROOT

# Acceptance envelope: 128 MHz, bounded pools as published. RAM defaults to the
# accepted 32 MiB; --ram selects the emulator's slots (up to 4 x 32M = 128 MiB).
CLOCK = 128_000_000
# G5: reaping is staged one root per section, so the longest IRQ-excluded span is
# a single populate, load or root teardown (about 14.5 ms measured). 20 ms keeps
# roughly a third of headroom; this is a regression budget, not a real-time promise.
SECTION_BUDGET = 2_560_000
# Per-kind ceilings, about 1.5x the 2026-10-08 maxima, so one operation growing
# cannot hide under the shared budget. Kinds not listed use SECTION_BUDGET.
KIND_CEILINGS = {
    'reap_stage': 2_400_000,   # one dead root on the idle stack; measured 1,550,591
    'syscall_1': 2_400_000,    # EXIT plus one staged root; measured 1,555,589
    'syscall_48': 2_560_000,   # populate 64 KiB; measured 1,753,847
    'syscall_46': 800_000,     # unmap 16 pages; measured 538,123
    'syscall_43': 500_000,     # allocate 16 zeroed pages; measured 325,268
    'syscall_45': 160_000,     # map 16 pages; measured 101,395
    'timer_irq': 16_000,       # IRQ and context switch; measured 10,099
    'syscall_55': 33_000,      # timed call; measured 21,658
    'syscall_22': 24_000,      # accept; measured 15,173
    'syscall_23': 27_000,      # reply; measured 17,945
}


def ram_bytes(spec):
    sizes = {'1M': 1, '2M': 2, '4M': 4, '8M': 8, '16M': 16, '32M': 32}
    slots = spec.split(',')
    require(1 <= len(slots) <= 4 and all(slot in sizes for slot in slots), f'unsupported --ram {spec}')
    return sum(sizes[slot] for slot in slots) * 1024 * 1024


def counters(text):
    match = re.search(r'cycles (\d+)\s+retired (\d+)', text)
    require(match is not None, 'monitor lacks cumulative CPU counters')
    return tuple(map(int, match.groups()))


def iret_pc(data, symbols):
    points = [pc for pc in range(symbols['trapEntry.restore'], symbols['trapEntry.bad_stack'], 4)
              if disassemble(instruction(data, pc), pc) == 'iret']
    require(len(points) == 1, 'restore path lacks a unique IRET')
    return points[0]


def wfi_pc(data, symbols):
    points = [pc for pc in range(symbols['taskKernelResume'], symbols['taskKernelResume.dispatch'], 4)
              if disassemble(instruction(data, pc), pc) == 'wfi']
    require(len(points) == 1, 'idle loop lacks a unique WFI')
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
        self.wfi = wfi_pc(self.data, self.s)

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

    def note_timer(self, point):
        if self.last_timer is not None:
            self.timer_gaps.append(point[0] - self.last_timer)
        self.last_timer = point[0]

    def idle_stages(self, expected):
        """Resume staged teardown on the idle stack, measuring every stage.

        A stage runs from the idle loop's `mtcr status, r0` to either the
        stage-pending branch (more roots remain) or the WFI (last root). The IRQ
        window between stages may take a timer interrupt, measured as usual.
        """
        idle, selected = self.s['taskKernelResume.idle'], self.s['taskKernelResume.selected']
        self.cmd('del all')
        for pc in (idle, selected, self.wfi, self.s['trapEntry'], self.iret):
            self.cmd(f'b 0x{pc:X}')
        poll = trap = None
        stages = 0
        for _ in range(64):
            self.cmd('c')
            text = self.cmd('r')
            pc, now = Monitor.registers(text)['pc'], counters(text)
            if pc == idle:
                poll = now
            elif pc in (selected, self.wfi):
                require(poll is not None, 'idle stage ended without a start')
                self.samples.append(dict(kind='reap_stage', cycles=now[0] - poll[0], retired=now[1] - poll[1]))
                poll = None
                stages += 1
                if pc == self.wfi:
                    self.cmd('del all')
                    return stages
            elif pc == self.s['trapEntry']:
                require(Monitor.registers(text)['cause'] == 0, 'unexpected trap between reap stages')
                trap = now
                self.note_timer(now)
            elif pc == self.iret:
                require(trap is not None, 'IRET without a measured trap')
                self.samples.append(dict(kind='timer_irq', cycles=now[0] - trap[0], retired=now[1] - trap[1]))
                trap = None
        raise ValueError(f'staged teardown did not finish in {expected} stages')

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
        # The EXIT section committed one root; the other seven are staged from
        # the idle loop, one per section, with an IRQ window between them.
        roots = lambda: sum(self.field(slot, 'directory') == 0 for slot in range(1, 9))
        require(roots() == 1, 'EXIT section did not stage exactly one root')
        stages = self.idle_stages(7)
        require(stages == 7, f'expected seven idle stages, saw {stages}')
        for slot in range(1, 9):
            require(self.field(slot, 'directory') == 0 and self.field(slot, 'kernelStackBottom') == 0,
                    'staged reaping retained physical resources')
        # Idle WFI must subsequently deliver a real timer interrupt.
        trapped = self.stop(self.s['trapEntry'])
        require(trapped['cause'] == 0, 'idle timer did not progress after cleanup')
        self.finish_measurement()
        return stages + 1

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
    ram_bytes(args.ram)
    paths = dict(image=args.image, map=args.map, emulator=args.emulator, rom=args.rom)
    hashes = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    data, symbols = args.image.read_bytes(), symbols_from_map(args.map)
    layout = preflight(data, symbols)
    with ready_monitor(data, args.emulator, args.rom, args.timeout, extra_args=('--ram', args.ram, '--clock', '128M')) as opened:
        monitor, process, stdout, stderr = opened
        p = LatencyProbe(monitor, data, symbols, args.timeout, layout)
        try:
            p.start_service()
            # The guest's own record of installed RAM, not the command line.
            installed = monitor.words(symbols['kernelRamEnd'], 1)[0]
            require(installed == ram_bytes(args.ram), f'guest sees {installed} bytes of RAM, not {args.ram}')
            free_frames = monitor.words(symbols['memoryFreePages'], 1)[0]
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
            reap_sections = p.cleanup_all()
            summary = summarize(p.samples)
            maximum = max(row['maximum_cycles'] for row in summary.values())
            require(maximum <= SECTION_BUDGET, 'IRQ-disabled section exceeds acceptance budget')
            for kind, ceiling in KIND_CEILINGS.items():
                require(summary[kind]['maximum_cycles'] <= ceiling, f'{kind} exceeds its regression ceiling')
            require(summary['reap_stage']['samples'] == 7, 'staged reaping did not run seven idle stages')
            require(max(p.timer_gaps) <= SECTION_BUDGET + CLOCK // 100, 'timer progress exceeded budget')
            uart = uart_text(stdout)
            require('PANIC' not in uart, 'latency image panicked')
            report = dict(complete=True, measurement='executed CPU counters; trapEntry to IRET, including reaping',
                          clock_hz=CLOCK, ram_bytes=installed, ram_slots=args.ram, free_frames_at_start=free_frames, section_budget_cycles=SECTION_BUDGET, kind_ceilings=KIND_CEILINGS,
                          fairness=fairness, memory=memory, simultaneous_reaped_tasks=8, reap_sections=reap_sections, reap_stage_tasks=1,
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
    parser.add_argument('--ram', default='32M', help='emulator RAM slots, e.g. 32M or 32M,32M,32M,32M')
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

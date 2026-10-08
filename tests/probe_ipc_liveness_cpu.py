#!/usr/bin/env python3
"""A4 acceptance on LA/IX executable bytes and an existing WRM binary.

No emulator build or instruction patching. Trusted bootstrap/context fixtures
reuse the scheduler probe; expiry runs from the actual hardware timer IRQ.
"""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess

from probe_boot import ready_monitor, require
from probe_mmu_cpu import symbols_from_map
from probe_ipc_request_reply_cpu import RequestReplyProbe, preflight as ipc_preflight, negative
from probe_scheduler_cpu import DATA, C, uart_text
from run_ready import ROOT
from test_kernel import LAIX

TIMED, TRY_SEND, TRY_RECEIVE, TRY_ACCEPT, CANCEL, SLEEP = range(55, 61)
CASES = ('timeouts', 'boundary', 'death', 'fifo', 'cancel', 'cycle', 'watchdog', 'idle_sleep', 'stress')


class LivenessProbe(RequestReplyProbe):
    def start_service(self, count=6, deferred=0):
        self.count = count
        for slot in range(1, self.count + 1):
            require(self.call('taskCreateImage', self.s['userCodeStart'], self.s['userCodeEnd'], 0) == slot,
                    'could not construct fixture task')
        root = self.call('endpointBootstrapService', self.address(1, 'handles'), 1)
        self.tokens = {1: root}
        for slot in range(2, self.count + 1):
            self.tokens[slot] = self.call('handleCopy', self.address(1, 'handles'), root,
                                          self.address(slot, 'handles'), 1, slot, 1)
        self.second = self.call('endpointBootstrapService', self.address(2, 'handles'), 2)
        self.to_second = self.call('handleCopy', self.address(2, 'handles'), self.second,
                                   self.address(1, 'handles'), 2, 1, 1)
        self.raw = self.call('endpointBootstrap', self.address(1, 'handles'), 1)
        self.raw_peer = self.call('handleCopy', self.address(1, 'handles'), self.raw,
                                  self.address(2, 'handles'), 1, 2, 3)
        for slot in range(1, self.count + 1):
            require(self.call('taskControlBootstrap', 6, slot, 63) == 1, 'scoped control grant failed')
            require(self.call('taskInstallRuntimeStart', slot, 0, 0, 0) == 1, 'startup install failed')
        for slot in range(1, self.count + 1):
            if slot != deferred:
                require(self.call('taskPublish', slot) == 1, 'fixture publication failed')
                self.seed(slot, self.busy)
        self.endpoint = self.m.words(self.address(1, 'handles') + ((root & 255) - 1) * 16, 1)[0]
        require(self.endpoint_field('references') == self.count, 'incorrect initial reference count')
        self.bridge()
        self.prepare(self.s['taskStart'], (1000000,))
        self.stop(self.busy)
        self.check_queue(1)
        self.expiries = 0

    def kernel_api(self, name, *args):
        returned = self.tick_return()
        slot = self.running()
        original = self.context(slot)
        self.inject_call(slot, returned, name, args)
        self.stop(self.s[name])
        restored = self.stop(self.s['trapEntry.restore'])
        stack = self.m.words(restored['r30'], C['TF_SIZE'] // 4)
        require(stack == original, f'kernel fixture stack corrupted for {name}: ' +
                str([(i, hex(a), hex(b)) for i, (a, b) in enumerate(zip(stack, original)) if a != b]))
        result = restored['r1']
        regs = self.stop(self.busy)
        self.m.commands([f"wp 0x{self.address(slot, 'context') + i * 4:X} 0x{word:X}"
                         for i, word in enumerate(original)])
        require([regs[f'r{i}'] for i in range(32)] == original[:32],
                f'kernel fixture corrupted user context for {name}; selected={slot}, running={self.running()}')
        self.check_queue(self.running())
        self.check_pending_contexts()
        return result

    def cleared(self, slot):
        super().cleared(slot)
        require(self.field(slot, 'waitTimed') == 0 and
                self.m.words(self.address(slot, 'waitDeadline'), 2) == [0, 0],
                'terminal wait retained deadline')

    def timed(self, slot=2, seconds=1, token=None):
        self.put_bytes(slot, 0, b'request')
        self.syscall(slot, TIMED, self.tokens[slot] if token is None else token,
                     DATA, 7, DATA + 128, 32, seconds)
        self.ipc_calls += 1

    def expire(self, slots):
        deadlines = {slot: self.m.words(self.address(slot, 'waitDeadline'), 2) for slot in slots}
        trapped = self.stop(self.s['ipc__ipcFinishWait'])
        require(trapped['cause'] == 0 and trapped['status'] & 16 and trapped['r4'] == 1,
                'expiry did not arrive through a hardware timer IRQ')
        require(trapped['r2'] in (negative(110), 0), 'timer chose wrong completion')
        now = self.devices()['count']
        require(all(((now - (lo | hi << 32)) & 0xFFFFFFFFFFFFFFFF) < 1 << 63
                    for lo, hi in deadlines.values()), 'timer expired early')
        self.stop(self.dispatch_return)
        self.check_pending_contexts()
        self.resume_busy()
        for slot in slots:
            require(self.field(slot, 'state') in (1, 2), 'expired task remained blocked')
            self.cleared(slot)
        self.expiries += len(slots)

    def timeouts(self):
        for accepted in (False, True):
            self.timed()
            token = self.accept() if accepted else (self.field(2, 'ipcCallGeneration') << 12) | 2
            self.waiting(2, 6 if accepted else 4)
            before = self.bytes(2, 128, 32)
            self.expire((2,))
            require(self.result(2) == (negative(110), 0), 'wrong timeout result')
            require(self.reply(token) == (negative(9), 0) and self.bytes(2, 128, 32) == before,
                    'late reply wrote expired response')
            self.timed(seconds=60)
            new = self.accept()
            require(new != token and self.reply(token) == (negative(9), 0), 'old reply completed later call')
            self.reply(new)
            require(self.result(2) == (8, 8), 'later call failed')
            require(self.endpoint_field('references') == 6, 'timeout leaked pin')
        return dict(pre_accept=True, post_accept=True, late_reply=True, next_generation=True,
                    natural_deadlines=True)

    def boundary(self):
        self.timed(seconds=60)
        token = self.accept()
        # Enter the reply handler under EXL, then place its deadline at COUNT.
        # This selects the reply-first serialization without modifying code.
        original_stop = self.stop
        def at_reply(pc):
            if pc == self.s['trapEntry']:
                regs = original_stop(pc)
                if regs['cause'] == 12 and regs['r9'] == 23:
                    original_stop(self.s['ipcReply'])
                    count = self.devices()['count']
                    self.write(self.address(2, 'waitDeadline'), count & 0xFFFFFFFF)
                    self.write(self.address(2, 'waitDeadline') + 4, count >> 32)
                return regs
            return original_stop(pc)
        self.stop = at_reply
        try:
            require(self.reply(token) == (8, 8), 'reply-first boundary failed')
        finally:
            self.stop = original_stop
        self.kernel_api('ipcTimerTick')
        require(self.result(2) == (8, 8), 'expiry overwrote reply')
        self.timed()
        token = self.accept()
        before = self.bytes(2, 128, 32)
        self.expire((2,))
        require(self.reply(token) == (negative(9), 0) and self.result(2) == (negative(110), 0) and
                self.bytes(2, 128, 32) == before, 'expiry-first boundary wrote stale response')
        require(self.endpoint_field('references') == 6, 'boundary leaked pin')
        return dict(reply_first_at_deadline=True, expiry_first=True, one_result=True, no_stale_write=True)

    def death(self):
        # Client termination before and after expiry, plus endpoint revocation
        # and service termination in each serialization. Six tasks remain bounded.
        self.timed(3, 60)
        token = self.accept()
        require(self.syscall(6, 40, 3, 9)[0] == 0, 'scoped client termination failed')
        self.cleared(3)
        require(self.reply(token) == (negative(9), 0), 'reply survived client death')
        self.timed(4)
        token = self.accept()
        self.expire((4,))
        require(self.syscall(6, 40, 4, 9)[0] == 0, 'termination after timeout failed')
        self.cleared(4)
        require(self.reply(token) == (negative(9), 0), 'late reply survived termination')
        self.timed(2)
        token = self.accept()
        self.expire((2,))
        require(self.syscall(1, 18, self.tokens[1])[0] == 0, 'destroy after expiry failed')
        require(self.result(2) == (negative(110), 0), 'destroy overwrote timeout')
        require(self.reply(token) == (negative(9), 0), 'destroy retained right')
        # A separate endpoint bound to task 2 survives the first destruction.
        self.timed(1, 60, self.to_second)
        length, token = self.syscall(2, 22, self.second, DATA, 32)
        require(length == 7, 'second endpoint accept failed')
        require(self.syscall(6, 40, 2, 9)[0] == 0, 'service termination failed')
        require(self.result(1) == (negative(32), 0), 'service death did not win')
        self.cleared(1)
        self.kernel_api('ipcTimerTick')
        require(self.result(1) == (negative(32), 0), 'expiry overwrote service death')
        return dict(client_death_both_orders=True, destroy_after_expiry=True,
                    service_death_before_expiry=True, rights_revoked=True)

    def fifo(self):
        for slot, seconds in ((2, 1), (3, 60), (4, 1), (5, 60)):
            self.timed(slot, seconds)
        # Align both short waits with the later deadline so one IRQ removes
        # oldest and middle entries; the CPU executes the bounded FIFO cleanup.
        deadline = self.m.words(self.address(4, 'waitDeadline'), 2)
        for i, word in enumerate(deadline):
            self.write(self.address(2, 'waitDeadline') + i * 4, word)
        self.expire((2, 4))
        for slot in (3, 5):
            token = self.accept()
            require(token & 4095 == slot, 'expiry changed unaffected FIFO order')
            self.reply(token)
        require(self.endpoint_field('references') == 6 and self.endpoint_field('senderCount') == 0,
                'FIFO cleanup leaked waits')
        return dict(oldest_and_middle=True, unaffected_fifo=True)

    def cancel(self):
        for accepted in (False, True):
            self.timed(seconds=60)
            token = self.accept() if accepted else 0
            require(self.syscall(3, CANCEL, 2)[0] == negative(1), 'foreign cancellation succeeded')
            require(self.syscall(6, CANCEL, 2)[0] == 0 and self.result(2) == (negative(125), 0),
                    'scoped cancellation failed')
            self.cleared(2)
            require(self.syscall(6, CANCEL, 2)[0] == negative(11), 'cancel completed twice')
            self.kernel_api('ipcTimerTick')
            require(self.result(2) == (negative(125), 0), 'timer overwrote cancellation')
            if token:
                require(self.reply(token) == (negative(9), 0), 'cancelled reply remained valid')
        require(self.endpoint_field('references') == 6, 'cancel leaked pin')
        return dict(scoped_authority=True, queued_and_accepted=True, repeat_rejected=True)

    def cycle(self):
        self.timed(1, 1, self.to_second)
        self.timed(2)
        # Both single-threaded services are blocked calling one another.
        self.waiting(2, 4)
        require(self.field(1, 'state') == 4, 'A-to-B-to-A did not block')
        deadline = self.m.words(self.address(2, 'waitDeadline'), 2)
        for i, word in enumerate(deadline):
            self.write(self.address(1, 'waitDeadline') + i * 4, word)
        self.expire((1, 2))
        require(self.result(1) == (negative(110), 0) and self.result(2) == (negative(110), 0),
                'cycle failed to restore progress')
        require(self.endpoint_field('references') == 6, 'cycle leaked pin')
        return dict(a_b_a=True, both_clients_resume=True)

    def watchdog(self):
        for number in (TRY_SEND, TRY_RECEIVE):
            require(self.syscall(1, number, self.raw, DATA, 4) == (negative(11), 0), 'raw try blocked')
        require(self.syscall(1, TRY_ACCEPT, self.tokens[1], DATA, 32) == (negative(11), 0),
                'try accept blocked')
        self.timed(seconds=60)
        token = self.accept()
        self.syscall(6, SLEEP, 1)
        require(self.field(6, 'state') == 4 and self.field(6, 'waitReason') == 8, 'watchdog did not sleep')
        require(self.kernel_api('taskWake', 6) == 0, 'generic wake bypassed sleep')
        self.expire((6,))
        require(self.result(6) == (0, 0), 'watchdog sleep failed')
        require(self.syscall(6, CANCEL, 2)[0] == 0, 'watchdog could not cancel stalled call')
        require(self.syscall(6, 40, 1, 9)[0] == 0, 'watchdog could not stop live looping server')
        self.cleared(2)
        require(self.result(2) == (negative(125), 0), 'death overwrote watchdog cancellation')
        require(self.syscall(6, 23, token, DATA, 1) == (negative(9), 0), 'looping service left right live')
        return dict(nonblocking=True, sleep_timer=True, infinite_loop_server=True,
                    scoped_cancel_and_terminate=True)

    def idle_sleep(self):
        for slot in range(1, 6):
            self.syscall(slot, SLEEP, 60)
        original_resume = self.resume_busy
        def observe_idle():
            self.stop(self.s['taskKernelResume.idle'])
            require(self.m.words(self.s['currentTask'], 1) == [self.s['idleTask']],
                    'all sleeping tasks did not select idle')
            require(all(self.field(slot, 'state') == 4 for slot in range(1, 7)),
                    'task was ready before idle sleep expiry')
            return self.regs()
        self.resume_busy = observe_idle
        try:
            self.syscall(6, SLEEP, 1)
        finally:
            self.resume_busy = original_resume
        # With no user runnable, actual idle WFI must deliver the timer IRQ.
        self.expire((6,))
        require(self.result(6) == (0, 0), 'idle timer failed to resume watchdog')
        for slot in range(1, 6):
            require(self.syscall(6, CANCEL, slot)[0] == 0, 'could not release sleeping peer')
            self.cleared(slot)
        return dict(all_users_blocked=True, idle_wfi=True, hardware_timer_wake=True)

    def stress(self, iterations):
        for index in range(iterations):
            slot = 2 + index % 2
            self.timed(slot)
            token = self.accept() if index % 2 else (self.field(slot, 'ipcCallGeneration') << 12) | slot
            self.expire((slot,))
            require(self.result(slot) == (negative(110), 0), 'stress timeout lost')
            require(self.reply(token) == (negative(9), 0), 'stress accepted stale reply')
            require(self.endpoint_field('references') == 6, 'stress leaked references')
            if (index + 1) % 8 == 0:
                print(f'  stress: {index + 1} natural CPU deadlines', flush=True)
        return dict(iterations=iterations, natural_deadlines=True, timer_entry=True,
                    looping_servers=True, no_pin_leaks=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--iterations', type=int, default=32)
    parser.add_argument('--case', action='append', choices=CASES)
    parser.add_argument('--log-dir', type=Path, default=LAIX / 'build/acceptance/ipc-liveness')
    args = parser.parse_args()
    require(args.timeout > 0 and args.iterations > 0, 'timeout and iterations must be positive')
    paths = dict(image=args.image.resolve(), map=args.map.resolve(),
                 emulator=args.emulator.resolve(), rom=args.rom.resolve(), probe=Path(__file__).resolve())
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    report = dict(complete=False, acceptance_complete=False,
                  method='LA/IX image; existing WRM binary; real timer IRQ; context/data fixtures',
                  artifacts={name: dict(path=str(paths[name]), sha256=value) for name, value in hashes.items()},
                  results=[])
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        data = paths['image'].read_bytes()
        symbols = symbols_from_map(paths['map'])
        required = {'ipcCallTimed', 'ipcTimerTick', 'ipc__ipcFinishWait', 'ipcSupervisorCancel', 'ipcSleep'}
        require(required <= symbols.keys(), 'image lacks A4 deadline implementation')
        layout = ipc_preflight(data, symbols)
        for case in args.case or CASES:
            with ready_monitor(data, paths['emulator'], paths['rom'], args.timeout) as opened:
                monitor, process, stdout, stderr = opened
                monitor.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                p = None
                try:
                    p = LivenessProbe(monitor, data, symbols, args.timeout, layout)
                    p.start_service()
                    result = p.stress(args.iterations) if case == 'stress' else getattr(p, case)()
                    require('PANIC' not in uart_text(stdout), 'kernel panic')
                    report['results'].append(dict(case=case, **result, expiries=p.expiries,
                                                  observed_timer_switches=p.timer_switches,
                                                  preserved_context=True, unique_ready_queue=True))
                    print(f'PASS {case}', flush=True)
                finally:
                    if p:
                        (args.log_dir / f'{case}.monitor.txt').write_text('\n'.join(p.log))
                    (args.log_dir / f'{case}.uart.txt').write_text(uart_text(stdout))
                    stderr.seek(0)
                    (args.log_dir / f'{case}.emulator.txt').write_text(stderr.read())
        require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name]
                    for name, path in paths.items()), 'probe changed input artifacts')
        report['complete'] = True
        report['acceptance_complete'] = {r['case'] for r in report['results']} == set(CASES)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report['error'] = str(error)
        print(f'FAIL IPC liveness CPU: {error}', flush=True)
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

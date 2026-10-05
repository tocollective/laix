#!/usr/bin/env python3
"""Standalone Raw rendezvous on existing CPU artifacts, without generating code."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess

from probe_boot import ready_monitor, require
from probe_mmu_cpu import symbols_from_map
from probe_ipc_liveness_cpu import LivenessProbe, CANCEL
from probe_ipc_request_reply_cpu import preflight, negative
from probe_scheduler_cpu import DATA, PAGE, uart_text
from run_ready import ROOT
from test_kernel import check_m, LAIX

CASES = ('send-first', 'receive-first', 'capacity', 'rights', 'cancel', 'lifecycle', 'stress')


class RawProbe(LivenessProbe):
    def start_raw(self):
        self.start_service()
        self.raw_tokens = {1: self.raw, 2: self.raw_peer}
        for slot in (3, 4, 5):
            self.raw_tokens[slot] = self.kernel_api('handleCopy', self.address(1, 'handles'), self.raw,
                                                   self.address(slot, 'handles'), 1, slot, 3)
        handle_size = check_m(LAIX / 'src/ipc/objects.m')[0].scope['Handle'].type.size
        self.endpoint = self.m.words(self.address(1, 'handles') + ((self.raw & 255) - 1) * handle_size, 1)[0]
        require(self.endpoint_field('mode') == 0 and self.endpoint_field('references') == 5,
                'Raw bootstrap reference baseline differs')

    def empty(self):
        require(self.endpoint_field('references') == 5 and
                self.endpoint_field('senderCount') == self.endpoint_field('receiverCount') == 0,
                'Raw exchange leaked references or wait records')

    def exchange(self, receive_first=False, message=b'\x00\xffRaw\x80'):
        self.put_bytes(1, 0, message)
        self.put_bytes(2, 0, b'z' * 32)
        first, second = ((2, 20), (1, 19)) if receive_first else ((1, 19), (2, 20))
        self.syscall(first[0], first[1], self.raw_tokens[first[0]], DATA, 32 if first[1] == 20 else len(message))
        self.waiting(first[0], 3 if first[1] == 20 else 2)
        if not receive_first:
            self.put_bytes(1, 0, b'!' * len(message))
        self.syscall(second[0], second[1], self.raw_tokens[second[0]], DATA, 32 if second[1] == 20 else len(message))
        require(self.result(1) == self.result(2) == (len(message), len(message)), 'wrong Raw result')
        require(self.bytes(2, 0, 32) == message + b'z' * (32 - len(message)), 'Raw payload/canary mismatch')
        self.cleared(1)
        self.cleared(2)
        self.empty()
        return dict(receive_first=receive_first, snapshot=True, destination_canary=True)

    def capacity(self):
        for slot, payload in ((2, b'first'), (3, b'second')):
            self.put_bytes(slot, 0, payload)
            self.syscall(slot, 19, self.raw_tokens[slot], DATA, len(payload))
        before = self.bytes(1, 0, 32)
        require(self.syscall(1, 20, self.raw, DATA, 4) == (negative(90), 5), 'small capacity consumed sender')
        require(self.syscall(1, 20, self.raw, DATA, 33) == (negative(90), 0), 'excess capacity accepted')
        require(self.bytes(1, 0, 32) == before and self.endpoint_field('senderCount') == 2,
                'capacity rejection changed bytes or FIFO')
        for payload in (b'first', b'second'):
            require(self.syscall(1, 20, self.raw, DATA, 32) == (len(payload), len(payload)), 'retry failed')
            require(self.bytes(1, 0, len(payload)) == payload, 'Raw FIFO reordered messages')
        self.empty()
        return dict(small_retry=True, maximum_capacity=32, fifo=True)

    def rights(self):
        send_only = self.kernel_api('handleCopy', self.address(1, 'handles'), self.raw,
                                    self.address(4, 'handles'), 1, 4, 1)
        require(self.syscall(4, 20, send_only, DATA, 32) == (negative(1), 0), 'send grant received')
        require(self.syscall(2, 19, self.raw_peer + 256, DATA, 0) == (negative(9), 0), 'stale handle accepted')
        for number, address, size, errno in ((19, DATA, 33, 90), (19, DATA + 2 * PAGE - 1, 2, 14),
                                             (20, 0, 1, 14)):
            actual = self.syscall(1, number, self.raw, address, size)
            require(actual == (negative(errno), 0), f'invalid Raw range accepted: syscall={number}, address={address:X}, result={actual}')
        require(self.syscall(4, 16, send_only)[0] == 0, 'attenuated grant close failed')
        self.empty()
        return dict(attenuation=True, stale_generation=True, invalid_buffers=True)

    def cancel(self):
        for number in (19, 20):
            for slot in (2, 3, 4):
                self.syscall(slot, number, self.raw_tokens[slot], DATA, 1)
            require(self.syscall(5, CANCEL, 3)[0] == negative(1), 'foreign cancellation allowed')
            require(self.syscall(6, CANCEL, 3)[0] == 0 and self.result(3) == (negative(125), 0), 'middle cancellation failed')
            require(self.syscall(6, CANCEL, 3)[0] == negative(11), 'wait completed twice')
            self.cleared(3)
            for slot in (2, 4):
                self.syscall(1, 20 if number == 19 else 19, self.raw, DATA, 1)
                require(self.result(slot) == (1, 1), 'cancellation disturbed surviving FIFO')
            self.empty()
        return dict(both_wait_sides=True, scoped_authority=True, middle_fifo=True, exactly_once=True)

    def lifecycle(self):
        self.syscall(2, 19, self.raw_peer, DATA, 1)
        self.syscall(3, 19, self.raw_tokens[3], DATA, 1)
        self.user_fault(1)
        for slot in (2, 3):
            require(self.result(slot) == (negative(32), 0), 'owner death retained Raw waiter')
            self.cleared(slot)
        require(self.field(1, 'directory') == 0 and self.field(1, 'kernelStackTop') == 0, 'dead Raw owner not reaped')
        require(self.syscall(4, 20, self.raw_tokens[4], DATA, 32) == (negative(32), 0), 'dead endpoint accepted receive')
        for slot in (2, 3, 4, 5):
            require(self.syscall(slot, 16, self.raw_tokens[slot])[0] == 0, 'dead endpoint grant close failed')
        require(self.endpoint_field('references') == 0 and self.endpoint_field('state') == 0, 'Raw slot not reclaimed')
        return dict(actual_user_fault=True, queued_clients_cancelled=True, endpoint_reclaimed=True)

    def stress(self, rounds):
        for index in range(rounds):
            self.exchange(bool(index % 2), bytes((index + n) & 255 for n in range(32)))
            if (index + 1) % 32 == 0:
                print(f'  Raw: {index + 1} exchanges', flush=True)
        return dict(exchanges=rounds, both_orders=True, maximum_messages=True, timer_preemption=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--rounds', type=int, default=128)
    parser.add_argument('--case', action='append', choices=CASES)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    paths = dict(image=args.image, map=args.map, emulator=args.emulator.resolve(), rom=args.rom.resolve())
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    report = dict(complete=False, sha256=hashes, results=[])
    try:
        require(args.timeout > 0 and args.rounds > 0, 'positive timeout and rounds required')
        data, symbols = args.image.read_bytes(), symbols_from_map(args.map)
        layout = preflight(data, symbols)
        for case in args.case or CASES:
            with ready_monitor(data, paths['emulator'], paths['rom'], args.timeout) as opened:
                monitor, process, stdout, stderr = opened
                monitor.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                p = RawProbe(monitor, data, symbols, args.timeout, layout)
                try:
                    p.start_raw()
                    result = p.exchange(case == 'receive-first') if case.endswith('-first') else (
                        p.stress(args.rounds) if case == 'stress' else getattr(p, case)())
                    require('PANIC' not in uart_text(stdout), 'Raw caused kernel panic')
                    report['results'].append(dict(case=case, timer_irqs=p.timer_switches, **result))
                finally:
                    (args.log_dir / f'{case}.monitor.txt').write_text('\n'.join(p.log))
                    (args.log_dir / f'{case}.uart.txt').write_text(uart_text(stdout))
                    stderr.seek(0)
                    (args.log_dir / f'{case}.emulator.txt').write_text(stderr.read())
            print('PASS Raw CPU: ' + case, flush=True)
        require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()), 'Raw inputs changed')
        report['complete'] = True
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        import traceback
        traceback.print_exc()
        report['error'] = str(error)
        print('FAIL Raw CPU: ' + str(error), flush=True)
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

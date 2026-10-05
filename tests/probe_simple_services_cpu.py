#!/usr/bin/env python3
"""CPU checks of ready simple-service images using an unchanged emulator/ROM."""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from probe_boot import ready_monitor, require, Monitor
from probe_screen_cpu import ScreenProbe
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import uart_text
from source_m import LAYOUT as C
from run_ready import ROOT


class SimpleProbe(ScreenProbe):
    def prepare(self):
        self.m.receive()
        self.stop(self.s['taskStart'])
        require([self.field(i, 'state') for i in range(1, 5)] == [1] * 4, 'tasks not published')
        require([self.field(i, 'deviceRights') for i in range(1, 5)] == [8, 16, 0, 0], 'wrong device rights')
        self.records = [self.m.words(self.field(i, 'bootPage'), 16) for i in range(1, 5)]
        for id, record in enumerate(self.records, 1):
            require(record[:3] == [C['START_MAGIC'], 2, 64] and record[4] == id, 'wrong startup ABI')
            root = self.field(id, 'directory')
            require(self.leaf(root, C['START_BLOCK_VA']) & 31 == 19, 'startup not RO/NX')
            for device in range(C['IO_BASE'], C['IO_BASE'] + 17 * 4096, 4096):
                require(self.leaf(root, device) & 24 == 0, 'MMIO has user or execute permission')

    def finish(self, expected=0):
        for _ in range(256):
            self.stop(self.s['taskKernelResume.idle'])
            if self.field(4, 'state') == 3:
                require(self.field(4, 'exitCode') == expected and not self.field(4, 'faulted'), f'wrong application exit: expected {expected}, got {self.field(4, "exitCode")}')
                return
        raise ValueError('application did not terminate')

    def natural(self, image):
        regs = self.stop(self.s['taskFinish'])
        require(self.m.words(self.s['currentTask'], 1)[0] == self.s['tasks'] + 3 * self.size, 'unexpected service death')
        require(regs['r2'] == 0 and regs['r3'] == 0, 'application failed before exit')
        app = symbols_from_map(self.services / 'simple-application.map')
        root = self.field(4, 'directory')
        va = app['application__simpleProof']
        physical = (self.leaf(root, va) & ~4095) + (va & 4095)
        data = struct.pack('<4I', *self.m.words(physical, 4))
        first = struct.unpack_from('<I', image, 4)[0] * 512
        require(data == image[first:first + 16], 'IPC file payload differs from on-disk bitmap')
        self.finish()
        require([self.field(i, 'state') for i in (1, 2, 3)] == [4] * 3, 'services did not return to blocked accept')
        require([self.field(i, 'waitReason') for i in (1, 2, 3)] == [5] * 3, 'service spins instead of accepting')
        require(self.m.words(self.s['service_devices__deviceBounce'], 1)[0] == 0, 'DMA buffer not released')

    def fault_service(self, id):
        self.patch_context(id, 0)
        self.finish(2 if id == 1 else 3)
        require(self.field(id, 'faulted') == 1 and self.field(id, 'exitCode') == 8, 'expected fetch page fault')
        require(self.field(id, 'reaped') == 1, 'faulted service not reaped')
        peers = (2, 3) if id == 1 else (1, 3) if id == 2 else (1, 2)
        # File service exits after forwarding the terminal disk failure.
        for peer in peers:
            if id == 2 and peer == 3:
                require(self.field(peer, 'state') == 3, 'file service did not terminate after disk death')
            else:
                require(self.field(peer, 'state') == 4, 'unrelated service did not continue')

    def fault_inflight(self):
        self.stop(self.s['deviceSubmit'])
        require(self.field(3, 'waitReason') == 6 and self.field(4, 'waitReason') == 6, 'clients not awaiting replies')
        regs = self.stop(self.s['trap__deviceResult'])
        require(self.m.words(self.s['service_devices__deviceBounce'], 1)[0] != 0, 'DMA reservation missing')
        frame = regs['r1']
        self.log.append(self.m.command(f'wp 0x{frame + C["TF_EPC"]:X} 0'))
        self.finish(4)
        require(self.field(2, 'faulted') == 1 and self.field(2, 'exitCode') == 8, 'disk did not fault')
        require(self.field(2, 'reaped') == 1 and self.field(3, 'state') == 3, 'failure cleanup incomplete')
        require(self.m.words(self.s['service_devices__deviceBounce'], 1)[0] == 0, 'late DMA buffer not released')

    def cancelled_canary(self):
        from probe_screen_cpu import user_instructions
        self.stop(self.s['deviceSubmit'])
        regs = self.stop(self.s['trap__deviceResult'])
        bounce = self.m.words(self.s['service_devices__deviceBounce'], 1)[0]
        require(bounce != 0 and regs['r2'] > 0, 'missing DMA operation')
        token = regs['r2']
        refs = self.m.words(self.s['memory__pageReferences'], 1)[0] + (bounce // 4096) * 4
        owners = self.m.words(self.s['memory__pageOwners'], 1)[0] + (bounce // 4096) * 4
        pc = next(pc for pc, text in user_instructions(self.services / 'disk.elf') if text == 'syscall')
        frame = regs['r1']
        # Invoke the real user cancellation syscall before its IRQ wait. Only
        # saved user registers and unused bytes in its broker page are changed.
        self.log.append(self.m.commands([
            f'wp 0x{bounce + 512:X} 0xC0FFEE', f'wp 0x{bounce + 4092:X} 0xC0FFEE',
            f'wp 0x{frame + C["TF_EPC"]:X} 0x{pc:X}',
            f'wp 0x{frame + C["TF_R9"]:X} {C["SYS_DEVICE_CANCEL"]}',
            f'wp 0x{frame + C["TF_R1"]:X} {token}',
        ]))
        self.log.append(self.m.command('del all'))
        cancellation = self.stop(self.s['deviceCancel'])
        require(cancellation['r2'] == token, 'wrong cancellation instance')
        self.log.append(self.m.command('del all'))
        self.stop(self.s['deviceReap'])
        info = self.m.command('info')
        self.log.append(info)
        require(' busy' in next(line for line in info.splitlines() if line.startswith('disk0')),
                'cancellation fixture did not observe real in-flight DMA: ' + info)
        require(self.m.words(refs, 1) == [1] and self.m.words(owners, 1) == [0xFFFFFFFE],
                'logical cancellation released the DMA pin')
        from test_kernel import check_m, LAIX
        operation = next(module.scope['DeviceOperation'].type for module in check_m(LAIX/'src/drivers/service_devices.m')
                         if 'DeviceOperation' in module.scope and module.scope['DeviceOperation'].type)
        address = self.s['service_devices__deviceOperation'] + operation.field('cancelled').offset
        word = self.m.words(address & ~3, 1)[0]
        require((word >> ((address & 3) * 8)) & 255 == 1, 'operation not logically cancelled')
        self.log.append(self.m.command('del all'))
        self.log.append('Awaiting physical quiescence and buffer release')
        release = self.stop(self.s['service_devices__deviceReleaseBuffer'])
        info = self.m.command('info')
        self.log.append(info)
        require(' busy' not in next(line for line in info.splitlines() if line.startswith('disk0')),
                'release before physical quiescence')
        require(self.m.words(refs, 1) == [1] and self.m.words(owners, 1) == [0xFFFFFFFE],
                'cancelled physical buffer reused before quiescence')
        require(self.m.words(bounce + 512, 1) == [0xC0FFEE] and self.m.words(bounce + 4092, 1) == [0xC0FFEE],
                'late DMA crossed its reservation')
        self.log.append(self.m.command('del all'))
        self.log.append('Awaiting trusted buffer-release return')
        self.stop(release['r31'])
        require(self.m.words(refs, 1) == [0] and self.m.words(owners, 1) == [0], 'cancelled buffer pin leaked')

    def denied(self, number):
        from probe_screen_cpu import user_instructions
        pc = next(pc for pc, text in user_instructions(self.services / 'simple-application.elf') if text == 'syscall')
        self.patch_context(4, pc, [(9, number), (1, C['START_DATA_VA']), (2, 16)])
        for _ in range(200):
            regs = self.stop(pc + 4)
            if regs['ptbr'] == self.field(4, 'ptbr'):
                require(regs['status'] & 4 != 0 and regs['r1'] == 0xFFFFFFFF, 'client gained device authority')
                return
        raise ValueError('client did not resume from denied operation')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--services', type=Path, default=ROOT / 'laix/build/services')
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=ROOT / 'laix/build/acceptance/simple-services')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--case', action='append', choices=['natural', 'input-fault', 'disk-fault', 'file-fault', 'disk-inflight-fault', 'disk-cancel-canary', 'input-denied', 'disk-info-denied', 'disk-begin-denied', 'disk-finish-denied', 'disk-cancel-denied'])
    args = parser.parse_args()
    cases = args.case or ['natural', 'input-fault', 'disk-fault', 'file-fault', 'disk-inflight-fault', 'disk-cancel-canary', 'input-denied', 'disk-info-denied', 'disk-begin-denied', 'disk-finish-denied', 'disk-cancel-denied']
    args.log_dir.mkdir(parents=True, exist_ok=True)
    paths = {'image': args.image, 'map': args.map, 'emulator': args.emulator, 'rom': args.rom}
    for name in ('input', 'disk', 'files', 'simple-application'):
        paths[name] = args.services / (name + '.elf')
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    symbols = symbols_from_map(args.map)
    report = {'complete': False, 'cases': [], 'sha256': hashes, 'ram': '2M'}
    try:
        for case in cases:
            with ready_monitor(args.image.read_bytes(), args.emulator.resolve(), args.rom.resolve(), args.timeout,
                               full_image=True, extra_args=('--ram', '2M') +
                               (('--clock', '128M') if case == 'disk-cancel-canary' else ())) as opened:
                monitor, process, stdout, stderr = opened
                probe = SimpleProbe(monitor, symbols, args.services)
                probe.prepare()
                if case == 'natural':
                    probe.natural(args.image.read_bytes())
                elif case.endswith('-denied'):
                    probe.denied({'input-denied': 31, 'disk-info-denied': 70, 'disk-begin-denied': 71,
                                  'disk-finish-denied': 72, 'disk-cancel-denied': 73}[case])
                elif case == 'disk-cancel-canary':
                    probe.cancelled_canary()
                elif case == 'disk-inflight-fault':
                    probe.fault_inflight()
                else:
                    probe.fault_service({'input-fault': 1, 'disk-fault': 2, 'file-fault': 3}[case])
                uart = uart_text(stdout)
                require('PANIC' not in uart, 'service failure caused kernel panic')
                (args.log_dir / (case + '.uart.txt')).write_text(uart)
                (args.log_dir / (case + '.monitor.txt')).write_text('\n'.join(probe.log))
            report['cases'].append(case)
            print('PASS simple services CPU: ' + case, flush=True)
        require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()), 'input artifact changed')
        report['complete'] = True
    except (OSError, ValueError, StopIteration, KeyError) as exc:
        if 'probe' in locals():
            (args.log_dir / (case + '.monitor.txt')).write_text('\n'.join(probe.log))
        report['error'] = str(exc)
        print('FAIL simple services CPU: ' + str(exc), flush=True)
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Forced shared IRQ and actual removable-media events on existing CPU inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import struct
import tempfile

from probe_boot import ready_monitor, require
from probe_simple_services_cpu import SimpleProbe
from probe_screen_cpu import ScreenProbe
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import uart_text
from device_snapshot import edit_snapshot, swap_floppy, video_shared
from source_m import LAYOUT as C
from run_ready import ROOT


def shared(args, case):
    symbols = symbols_from_map(args.map)
    with ready_monitor(args.image.read_bytes(), args.emulator.resolve(), args.rom.resolve(), args.timeout,
                       full_image=True, extra_args=('--ram', '2M', '--clock', '128M', '--deterministic')) as opened:
        m, process, stdout, stderr = opened
        m.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        p = ScreenProbe(m, symbols, ROOT / 'laix/build/services')
        def stop(name):
            m.command('del all')
            return p.stop(symbols[name])
        try:
            p.prepare()
            if case == 'rearm-boundary':
                for _ in range(20):
                    regs = stop('irqComplete')
                    if regs['r1'] == 1:
                        break
                else:
                    raise ValueError('Screen did not reach its rearm boundary')
                edit_snapshot(m, p.log, video_shared)
                m.command('del all')
                returned = p.stop(regs['r31'])
                require(returned['r1'] == 0xFFFFFFF0, 'held shared causes did not reject rearm with EBUSY')
                info = m.command('info')
                p.log.append(info)
                # A successful retry must clear both causes before reenabling.
                for _ in range(20):
                    regs = stop('irqComplete')
                    if regs['r1'] == 1:
                        break
                else:
                    raise ValueError('Screen did not retry rearm')
                m.command('del all')
                returned = p.stop(regs['r31'])
                require(returned['r1'] == 0, 'Screen did not acknowledge both shared causes and retry')
            else:
                for _ in range(100):
                    regs = stop('irqWait')
                    if regs['r2'] == p.records[0][15]:
                        break
                else:
                    raise ValueError('Screen did not reach its wait boundary')
                from test_kernel import LAIX, check_m
                grant = next(module.scope['IrqGrant'].type for module in check_m(LAIX / 'src/drivers/irq.m')
                             if 'IrqGrant' in module.scope and module.scope['IrqGrant'].type)
                address = symbols['irq__irqGrants'] + C['VIDEO_IRQ'] * grant.size
                flags = address + grant.field('pending').offset
                require(grant.field('timed').offset == grant.field('pending').offset + 3, 'IRQ fixture ABI mismatch')
                # Select the armed, no-notification precondition under EXL.
                # Owner/generation and user instructions are unchanged. Snapshot
                # supplies the level at exactly the wait publication boundary.
                p.log.append(m.command(f'wp 0x{flags:X} 0'))
                edit_snapshot(m, p.log, lambda data: video_shared(data, arm=True))
                for _ in range(100):
                    notification = stop('irqNotify')
                    if notification['r1'] == C['VIDEO_IRQ']:
                        break
                else:
                    raise ValueError('boundary video notification missing')
                require(p.field(1, 'state') == 4 and p.field(1, 'waitReason') == 7,
                        'IRQ arrived before wait publication')
                require(notification['r1'] == C['VIDEO_IRQ'] and notification['cause'] == 0,
                        'asserted wait boundary did not deliver the real video IRQ')
                m.command('del all')
                p.stop(notification['r31'])
                from probe_scheduler_cpu import SchedulerProbe
                info = m.command('info')
                p.log.append(info)
                devices = SchedulerProbe.parse_devices(info)
                require(devices['lines'] & (1 << C['VIDEO_IRQ']) and not devices['enable'] & (1 << C['VIDEO_IRQ']),
                        'unacknowledged shared level was not held masked')
            m.command('del all')
            p.natural()
            require('PANIC' not in uart_text(stdout), 'IRQ boundary caused panic')
            return dict(case=case, complete=True, shared_done_vblank=True,
                        physical_level=True, source='snapshot device-state timing fixture',
                        application_progress=True, natural_framebuffer=True)
        finally:
            (args.log_dir / f'{case}.monitor.txt').write_text('\n'.join(p.log))
            (args.log_dir / f'{case}.uart.txt').write_text(uart_text(stdout))
            stderr.seek(0)
            (args.log_dir / f'{case}.emulator.txt').write_text(stderr.read())


def medium(args, case):
    symbols = symbols_from_map(args.map)
    data = args.image.read_bytes()
    boot_bytes = struct.unpack_from('<I', data, 4)[0] * 512
    with tempfile.TemporaryDirectory(prefix='laix-media-') as directory:
        floppy = Path(directory) / 'approved.img'
        replacement = Path(directory) / 'replacement.img'
        # The service resource resides beyond the kernel boot span; a one-sector
        # independently approved floppy extent suffices for the 16-byte request.
        floppy.write_bytes(data)
        replacement.write_bytes(b'R' * len(floppy.read_bytes()))
        with ready_monitor(data, args.emulator.resolve(), args.rom.resolve(), args.timeout,
                           full_image=True, extra_args=('--ram', '2M', '--clock', '128M', '--deterministic',
                                                       '--floppy', str(floppy))) as opened:
            m, process, stdout, stderr = opened
            m.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            p = SimpleProbe(m, symbols, ROOT / 'laix/build/services')
            try:
                m.receive()
                p.only(symbols['bootstrapSimpleInit'])
                # Trusted boot policy selects a real removable drive and the
                # existing build-approved extent. No device MMIO is programmed.
                m.command(f'wp 0x{symbols["kernelBootInfo"] + 12:X} 0x{C["FLOPPY_BASE"]:X}')
                # Limit the approved resource to the actual removable capacity.
                # The build-issued extent is immutable, so keep the entire
                # declared resource when it fits the drive's 1.44 MiB limit.
                require(len(data) <= 1474560, 'Services media campaign needs a compact resource image')
                p.only(symbols['taskStart'])
                p.only(symbols['deviceSubmit'])
                p.only(symbols['trap__deviceResult'])
                info = m.command('info')
                p.log.append(info)
                require(' busy' in next(line for line in info.splitlines() if line.startswith('floppy')), 'media swap not in flight')
                edit_snapshot(m, p.log, lambda snapshot: swap_floppy(snapshot, replacement if case == 'media-replacement' else None))
                info = m.command('info')
                p.log.append(info)
                line = next(line for line in info.splitlines() if line.startswith('floppy'))
                require(('empty' in line or 'no disk' in line) if case == 'media-removal' else ' busy' not in line,
                        'actual media event did not stop old transfer')
                p.only(symbols['taskKernelResume.idle'])
                require(p.field(4, 'state') == 3 and p.field(4, 'exitCode') == 4, 'medium event did not fail the client')
                require(m.words(symbols['service_devices__deviceBounce'], 1) == [0], 'media event retained DMA pin')
                require(p.field(1, 'state') == 4, 'unrelated Input service died')
                require('PANIC' not in uart_text(stdout), 'media event caused panic')
                return dict(case=case, complete=True, actual_snapshot_media_api=True, changed_medium_denied=True,
                            in_flight=True, pins_released=True, unrelated_service=True)
            finally:
                (args.log_dir / f'{case}.monitor.txt').write_text('\n'.join(p.log))
                (args.log_dir / f'{case}.uart.txt').write_text(uart_text(stdout))
                stderr.seek(0)
                (args.log_dir / f'{case}.emulator.txt').write_text(stderr.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--case', action='append', required=True,
                        choices=('rearm-boundary', 'wait-boundary', 'media-removal', 'media-replacement'))
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    paths = dict(image=args.image, map=args.map, emulator=args.emulator, rom=args.rom)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    report = dict(complete=False, sha256=hashes, results=[])
    try:
        for case in args.case:
            report['results'].append(medium(args, case) if case.startswith('media') else shared(args, case))
            print('PASS device events CPU: ' + case, flush=True)
        require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()), 'device event inputs changed')
        report['complete'] = True
    except (ValueError, OSError, KeyError, StopIteration) as error:
        report['error'] = str(error)
        print('FAIL device events CPU: ' + str(error), flush=True)
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

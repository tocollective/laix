#!/usr/bin/env python3
"""CPU acceptance of existing screen-service images, with unchanged WRM/ROM.

Negative fixtures change saved user contexts only. They use existing user
instructions and never generate code, modify PTEs or program DMA in monitor.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import tempfile

from probe_boot import ready_monitor, Monitor, require
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import task_layout, uart_text
from source_m import LAYOUT as C
from run_ready import ROOT
from disasm import disassemble


def user_instructions(path):
    data = path.read_bytes()
    header = struct.unpack_from('<16sHHIIIIIHHHHHH', data)
    for i in range(header[10]):
        kind, offset, va, pa, files, mem, flags, align = struct.unpack_from('<8I', data, header[5] + i * 32)
        if kind == 1 and flags == 5:
            for n in range(0, files - files % 4, 4):
                pc = va + n
                yield pc, disassemble(struct.unpack_from('<I', data, offset + n)[0], pc)


class ScreenProbe:
    def __init__(self, monitor, symbols, service_dir):
        self.m, self.s, self.services = monitor, symbols, service_dir
        self.size, self.offsets = task_layout()
        self.log = []

    def address(self, id, field):
        return self.s['tasks'] + (id - 1) * self.size + self.offsets[field]

    def field(self, id, field):
        address = self.address(id, field)
        if field in ('faulted', 'queued', 'reaped'):
            word = self.m.words(address & ~3, 1)[0]
            return (word >> (8 * (address & 3))) & 255
        return self.m.words(address, 1)[0]

    def stop(self, pc):
        output = self.m.stop_at(pc)
        self.log.append(output)
        return Monitor.registers(output)

    def leaf(self, root, virtual):
        directory = self.m.words(root + 4 * (virtual >> 22), 1)[0]
        if directory & 1 == 0:
            return 0
        if directory & 14:
            return directory
        return self.m.words((directory & ~4095) + 4 * ((virtual >> 12) & 1023), 1)[0]

    def prepare(self):
        self.m.receive()
        self.stop(self.s['taskStart'])
        states = [self.field(i, 'state') for i in (1, 2, 3)]
        require(states == [1, 1, 1], 'services were not published: ' + str(states))
        require([self.field(i, 'deviceRights') for i in (1, 2, 3)] == [2, 4, 0], 'wrong device operation grants')
        self.records = [self.m.words(self.field(i, 'bootPage'), 16) for i in (1, 2, 3)]
        for id, record in enumerate(self.records, 1):
            require(record[0:3] == [C['START_MAGIC'], 2, 64] and record[4] == id, 'invalid startup record')
            root = self.field(id, 'directory')
            require(self.leaf(root, C['START_BLOCK_VA']) & 31 == 19, 'startup is not RO/NX')
            require(self.leaf(root, C['SERVICE_IMAGE_BASE']) & 31 == 27, 'program is not RX/U')
            for device in range(C['IO_BASE'], C['IO_BASE'] + 17 * 4096, 4096):
                require(self.leaf(root, device) & 24 == 0, 'physical MMIO alias has U or X')
            require(self.leaf(root, C['VRAM_BASE']) & 24 == 0, 'physical VRAM alias has U or X')
            for va, size, perms in [(C['SCREEN_VRAM_VA'], C['SCREEN_VRAM_BYTES'], 23),
                                    (C['SCREEN_VIDEO_VA'], 4096, 19)]:
                for offset in range(0, size, 4096):
                    leaf = self.leaf(root, va + offset)
                    require(leaf & 31 == (perms if id == 1 else 0), 'wrong per-service device alias')
                require(self.leaf(root, va + size) == 0, 'device grant exceeds its extent')
        require(self.records[0][12] != 0 and self.records[0][15] != 0 and self.records[1][15] != 0,
                'missing bitmap/IRQ grants')

    def patch_context(self, id, pc, regs=()):
        frame = self.address(id, 'context')
        commands = [f'wp 0x{frame + C["TF_EPC"]:X} 0x{pc:X}']
        commands += [f'wp 0x{frame + 4 * reg:X} 0x{value & 0xFFFFFFFF:X}' for reg, value in regs]
        self.log.append(self.m.commands(commands))

    def fault(self, id, target, fetch=False):
        image = 'screen' if id == 1 else 'application'
        if fetch:
            self.patch_context(id, target)
        else:
            for pc, text in user_instructions(self.services / (image + '.elf')):
                match = re.fullmatch(r'sw r(\d+), (-?\d+)\(r(\d+)\)', text)
                if match and int(match[3]) != 0:
                    self.patch_context(id, pc, [(int(match[3]), target - int(match[2]))])
                    break
            else:
                raise ValueError('no existing user store instruction')
        self.finish(fault_id=id)
        require(self.field(id, 'faulted') == 1 and self.field(id, 'exitCode') == (8 if fetch else 10), 'wrong CPU fault')
        frame = self.address(id, 'context')
        require(self.m.words(frame + C['TF_BADADDR'], 1)[0] == target, 'wrong CPU fault address')
        require(self.field(2, 'state') == 4, 'bitmap server did not continue after peer fault')

    def denied(self, number):
        pc = next(pc for pc, text in user_instructions(self.services / 'application.elf') if text == 'syscall')
        # The selected screen token remains unusable in the client's own TCB.
        self.patch_context(3, pc, [(9, number), (1, self.records[0][15]), (2, 0), (3, 0)])
        for _ in range(200):
            regs = self.stop(pc + 4)
            if regs['ptbr'] == self.field(3, 'ptbr'):
                require(regs['status'] & 4 != 0 and regs['r1'] == 0xFFFFFFFF, 'client gained device/IRQ authority')
                return
        raise ValueError('client did not resume from denied syscall')

    def finish(self, fault_id=0):
        for _ in range(256):
            self.stop(self.s['taskKernelResume.idle'])
            if self.field(3, 'state') == 3 and (fault_id == 0 or self.field(fault_id, 'state') == 3):
                return
        raise ValueError('application did not finish within bounded device waits')

    def natural(self):
        self.finish()
        state = [{name: self.field(id, name) for name in ('state', 'waitReason', 'exitCode', 'faulted')} for id in (1, 2, 3)]
        require(self.field(3, 'exitCode') == 0 and self.field(3, 'faulted') == 0, 'screen application failed: ' + str(state))
        require([self.field(i, 'state') for i in (1, 2)] == [4, 4] and
                [self.field(i, 'waitReason') for i in (1, 2)] == [5, 5], 'services did not return to blocked accept')
        # Compare the CPU-rendered first L with the selected disk font bitmap.
        font = (ROOT / 'laix/fonts/unifont-console.laf').read_bytes()
        count, pixels = struct.unpack_from('<I', font, 8)[0], struct.unpack_from('<I', font, 16)[0]
        index = next(i for i in range(count) if struct.unpack_from('<I', font, 32 + i * 8)[0] == ord('L'))
        bitmap = font[pixels + index * 32:pixels + (index + 1) * 32]
        # Monitor xp deliberately uses the instruction-fetch bus, which cannot
        # read VRAM. Save the paused machine and inspect its tagged VRAM bytes.
        with tempfile.TemporaryDirectory(prefix='laix-screen-cpu-') as directory:
            snapshot = Path(directory) / 'screen.snap'
            self.log.append(self.m.command('save ' + str(snapshot)))
            data = snapshot.read_bytes()
        require(data[:8] == b'WRMSNAP\0', 'invalid machine snapshot')
        # snap_video's prefix uses eight-byte numeric slots, a 256-entry
        # palette and an 8200-byte source-line buffer. Validate the offset
        # against both the raw cached glyph and its independently drawn pixels.
        video_tags = [match.start() for match in re.finditer(b'VID ', data)]
        relative = 4 + 4 * 8 + 256 * 8 + 2 + 14 * 8 + 2 + 6 * 8 + 8200 + 4 * 8
        positions = [tag + relative for tag in video_tags if tag + relative + 0x400000 <= len(data)]
        require(len(positions) == 1, 'snapshot has no unique bounded video section')
        framebuffer = data[positions[0]:positions[0] + 0x400000]
        require(framebuffer[640 * 480:640 * 480 + 32] == bitmap, 'CPU cache differs from the disk-backed glyph')
        for row in range(16):
            expected = bytes((bitmap[row * 2] >> (7 - col)) & 1 for col in range(8))
            actual = framebuffer[row * 640:row * 640 + 8]
            require(actual == expected, f'CPU framebuffer differs from the disk-backed glyph at row {row}: '
                    f'{actual.hex()} != {expected.hex()}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path)
    parser.add_argument('--services', type=Path, default=ROOT / 'laix/build/services')
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=ROOT / 'laix/build/acceptance/screen')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--case', action='append', choices=['natural', 'mmio-write', 'video-nx', 'vram-nx', 'font-nx', 'font-write', 'client-vram',
                        'irq-denied', 'rearm-denied', 'video-denied', 'dma-denied', 'dma-result-denied', 'cancel-denied', 'font-denied'])
    args = parser.parse_args()
    cases = args.case or ['natural', 'mmio-write', 'video-nx', 'vram-nx', 'font-nx', 'font-write', 'client-vram',
                         'irq-denied', 'rearm-denied', 'video-denied', 'dma-denied', 'dma-result-denied', 'cancel-denied', 'font-denied']
    args.log_dir.mkdir(parents=True, exist_ok=True)
    paths = {'image': args.image, 'map': args.map, 'emulator': args.emulator, 'rom': args.rom}
    paths.update({name: args.services / (name + '.elf') for name in ('screen', 'storage', 'application')})
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    symbols = symbols_from_map(args.map)
    report = {'complete': False, 'cases': [], 'sha256': hashes, 'ram': '2M'}
    try:
        for case in cases:
            with ready_monitor(args.image.read_bytes(), args.emulator.resolve(), args.rom.resolve(), args.timeout,
                               full_image=True, extra_args=('--ram', '2M')) as opened:
                monitor, process, stdout, stderr = opened
                probe = ScreenProbe(monitor, symbols, args.services)
                probe.prepare()
                if case == 'natural':
                    probe.natural()
                elif case in ('mmio-write', 'video-nx', 'vram-nx', 'font-nx', 'font-write', 'client-vram'):
                    id = 3 if case == 'client-vram' else 1
                    target = {'mmio-write': C['SCREEN_VIDEO_VA'], 'video-nx': C['SCREEN_VIDEO_VA'],
                              'vram-nx': C['SCREEN_VRAM_VA'], 'font-nx': C['SCREEN_FONT_VA'],
                              'font-write': C['SCREEN_FONT_VA'], 'client-vram': C['SCREEN_VRAM_VA']}[case]
                    probe.fault(id, target, case.endswith('-nx'))
                else:
                    number = {'irq-denied': 24, 'rearm-denied': 25, 'video-denied': 26, 'dma-denied': 71,
                              'dma-result-denied': 72, 'cancel-denied': 73, 'font-denied': 70}[case]
                    probe.denied(number)
                uart = uart_text(stdout)
                require('PANIC' not in uart, 'user service caused a kernel panic')
                (args.log_dir / (case + '.uart.txt')).write_text(uart)
                (args.log_dir / (case + '.monitor.txt')).write_text('\n'.join(probe.log))
            report['cases'].append(case)
            print('PASS screen CPU: ' + case, flush=True)
        require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()), 'input artifact changed')
        report['complete'] = True
    except (OSError, ValueError, StopIteration) as exc:
        report['error'] = str(exc)
        print('FAIL screen CPU: ' + str(exc), flush=True)
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

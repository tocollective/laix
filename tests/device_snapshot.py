"""Device timing fixtures through WRM's existing snapshot API; no code patches."""
from pathlib import Path
import re
import struct
import tempfile

from probe_boot import require
from source_m import LAYOUT as C


def rehash(data):
    value = 0xCBF29CE484222325
    for byte in data[:-8]:
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    struct.pack_into('<Q', data, len(data) - 8, value)


def edit_snapshot(monitor, log, edit):
    with tempfile.TemporaryDirectory(prefix='laix-device-fixture-') as directory:
        path = Path(directory) / 'timing.snap'
        log.append(monitor.command('save ' + str(path)))
        data = bytearray(path.read_bytes())
        require(data[:8] == b'WRMSNAP\0', 'unsupported snapshot magic')
        edit(data)
        rehash(data)
        path.write_bytes(data)
        response = monitor.command('load ' + str(path))
        log.append(response)
        require('loaded ' in response, 'device snapshot fixture rejected')


def disks(data):
    result = []
    # A bounded path followed by the known disk-state length distinguishes
    # structural disk tags from coincidental bytes in RAM or a framebuffer.
    for match in re.finditer(b'DISK', data):
        tag = match.start()
        if tag + 12 >= len(data):
            continue
        length = struct.unpack_from('<Q', data, tag + 4)[0]
        if length > 4096:
            continue
        fields = tag + 12 + length + 16
        end = fields + 1 + 6 * 8 + 2 + 512 + 3 * 8
        if end < len(data) and bytes(data[end:end + 4]) in (b'DISK', b'VID ', b'BEEP'):
            result.append((tag, length, fields, end))
    require(len(result) == 3, 'snapshot must have exactly HDD0, HDD1 and floppy disk records')
    return result


def delay_disk(data, ticks, index=0):
    tag, length, fields, end = disks(data)[index]
    require(data[fields + 49] == 1, 'delay fixture requires an active physical transfer')
    struct.pack_into('<Q', data, end - 16, ticks)


def swap_floppy(data, path=None):
    tag, length, fields, end = disks(data)[2]
    name = b'' if path is None else str(path).encode()
    state = bytearray(data[fields:end])
    # Snapshot loading invokes disk_insert/disk_eject for changed floppy paths.
    # Preserve that new medium's CHANGED cause; never resume the old DMA.
    state[0] = 1
    state[49:51] = b'\0\0'
    struct.pack_into('<Q', state, 25, 0)
    header = struct.pack('<Q', len(name)) + name + struct.pack('<QQ',
        0 if path is None else Path(path).stat().st_size // 512,
        0 if path is None else int(Path(path).stat().st_mtime))
    data[tag + 4:end] = header + state


def video_shared(data, arm=False):
    positions = []
    for match in re.finditer(b'VID ', data):
        tag = match.start()
        end = tag + 4 + 4 * 8 + 256 * 8 + 2 + 14 * 8 + 2 + 6 * 8 + 8200 + 4 * 8 + 0x400000
        if bytes(data[end:end + 4]) == b'DISK':
            positions.append(tag)
    require(len(positions) == 1, 'unsupported snapshot video structure')
    tag = positions[0]
    data[tag + 4 + 4 * 8 + 256 * 8 + 1] = 1  # VBLANK
    state = tag + 4 + 4 * 8 + 256 * 8 + 2 + 14 * 8
    require(data[state] == 0, 'shared cause fixture needs a quiescent video engine')
    data[state + 1] = 1  # DONE on the same IRQ line
    # Loading video does not recompute the PIC line; restore its asserted level.
    pic = next(match.start() for match in re.finditer(b'PIC ', data)
               if bytes(data[match.start() + 20:match.start() + 24]) == b'KBD ')
    lines = struct.unpack_from('<Q', data, pic + 4)[0]
    struct.pack_into('<Q', data, pic + 4, lines | (1 << C['VIDEO_IRQ']))
    if arm:
        enabled = struct.unpack_from('<Q', data, pic + 12)[0]
        struct.pack_into('<Q', data, pic + 12, enabled | (1 << C['VIDEO_IRQ']))

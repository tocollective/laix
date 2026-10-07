#!/usr/bin/env python3
"""WFS1: the LA/IX flat filesystem image (docs/FILESYSTEM.md).

One volume is a whole number of 512-byte sectors:

    sectors 0-1   metadata copy A
    sectors 2-3   metadata copy B
    sectors 4-    data: files are contiguous extents of whole sectors

A metadata copy is 1024 bytes, 256 little-endian words:

    word 0   magic 'WFS1'
    word 1   generation (the valid copy with the highest value is current)
    word 2   next file serial
    word 3   volume sectors
    word 4   first data sector (4)
    word 5   CRC-32 of the 1024 bytes with this word zeroed
    word 6-7 reserved, zero
    word 8+  31 entries of 8 words: name[16] NUL padded, size, first sector,
             capacity in sectors, serial. An empty name marks a free entry.

The service commits by writing the copy that is not current with generation + 1,
so a torn commit leaves the previous copy valid. This module is the reference
for the format: tests build images with it and parse what the M service wrote.
"""

import argparse
from pathlib import Path
import struct
import sys
import zlib

SECTOR = 512
META_SECTORS = 2
META_BYTES = SECTOR * META_SECTORS
DATA_START = 4
MAGIC = 0x31534657  # 'WFS1'
MAX_FILES = 31
NAME_BYTES = 16
ENTRY_WORDS = 8
HEADER_WORDS = 8
MIN_SECTORS = DATA_START + 1
MAX_SERIAL = (1 << 26) - 1  # ids are serial << 5 | slot


class FormatError(ValueError):
    pass


def valid_name(name):
    raw = name if isinstance(name, bytes) else name.encode('ascii')
    return 1 <= len(raw) <= NAME_BYTES and all(0x21 <= byte <= 0x7E for byte in raw)


def crc(table):
    zeroed = bytearray(table)
    zeroed[20:24] = bytes(4)
    return zlib.crc32(bytes(zeroed)) & 0xFFFFFFFF


def pack_table(generation, serial, sectors, entries):
    """entries: list of (name, size, first, capacity, serial) in slot order."""
    if len(entries) > MAX_FILES:
        raise FormatError('too many files')
    words = [0] * (META_BYTES // 4)
    words[0:5] = [MAGIC, generation, serial, sectors, DATA_START]
    for slot, entry in enumerate(entries):
        if entry is None:
            continue
        name, size, first, capacity, entry_serial = entry
        raw = name.encode('ascii') if isinstance(name, str) else name
        if not valid_name(raw):
            raise FormatError(f'invalid name {name!r}')
        base = HEADER_WORDS + slot * ENTRY_WORDS
        words[base:base + 4] = struct.unpack('<4I', raw.ljust(NAME_BYTES, b'\0'))
        words[base + 4:base + 8] = [size, first, capacity, entry_serial]
    table = bytearray(struct.pack('<256I', *words))
    table[20:24] = struct.pack('<I', crc(table))
    return bytes(table)


def parse_table(table):
    """Returns the decoded table, or raises FormatError."""
    if len(table) != META_BYTES:
        raise FormatError('metadata copy must be 1024 bytes')
    words = struct.unpack('<256I', table)
    if words[0] != MAGIC:
        raise FormatError('bad magic')
    if words[5] != crc(table):
        raise FormatError('bad checksum')
    if words[4] != DATA_START or words[6] or words[7] or words[1] == 0:
        raise FormatError('bad header')
    entries = []
    for slot in range(MAX_FILES):
        base = HEADER_WORDS + slot * ENTRY_WORDS
        raw = struct.pack('<4I', *words[base:base + 4])
        if raw[0] == 0:
            if any(words[base:base + ENTRY_WORDS]):
                raise FormatError(f'entry {slot} is free but not zero')
            entries.append(None)
            continue
        name = raw.rstrip(b'\0')
        if b'\0' in name or not valid_name(name) or raw[len(name):] != bytes(NAME_BYTES - len(name)):
            raise FormatError(f'entry {slot} has an invalid name')
        entries.append(dict(name=name.decode('ascii'), size=words[base + 4], first=words[base + 5],
                            capacity=words[base + 6], serial=words[base + 7]))
    return dict(generation=words[1], serial=words[2], sectors=words[3], entries=entries)


def check_table(table):
    """Structural invariants beyond the checksum; raises FormatError."""
    names = set()
    spans = []
    for slot, entry in enumerate(table['entries']):
        if entry is None:
            continue
        if entry['name'] in names:
            raise FormatError(f'duplicate name {entry["name"]}')
        names.add(entry['name'])
        if entry['capacity'] == 0 or entry['first'] < DATA_START or entry['first'] + entry['capacity'] > table['sectors']:
            raise FormatError(f'{entry["name"]}: extent outside the data area')
        if entry['size'] > entry['capacity'] * SECTOR:
            raise FormatError(f'{entry["name"]}: size beyond capacity')
        if not 0 < entry['serial'] < table['serial'] or entry['serial'] > MAX_SERIAL:
            raise FormatError(f'{entry["name"]}: serial not below the next serial')
        spans.append((entry['first'], entry['first'] + entry['capacity'], entry['name']))
    spans.sort()
    for (_, end, name), (start, _, other) in zip(spans, spans[1:]):
        if start < end:
            raise FormatError(f'{name} and {other} overlap')


def slots(image):
    """The two metadata copies of an image: each a decoded table or None."""
    result = []
    for slot in range(2):
        copy = image[slot * META_BYTES:(slot + 1) * META_BYTES]
        try:
            result.append(parse_table(copy))
        except FormatError:
            result.append(None)
    return result


def current(image):
    """(slot, table) of the valid copy with the highest generation."""
    best = None
    for slot, table in enumerate(slots(image)):
        if table is not None and (best is None or table['generation'] > best[1]['generation']):
            best = (slot, table)
    if best is None:
        raise FormatError('no valid metadata copy')
    return best


def read_file(image, entry):
    start = entry['first'] * SECTOR
    return bytes(image[start:start + entry['size']])


def files(image):
    """name -> bytes for the current committed state."""
    _, table = current(image)
    return {e['name']: read_file(image, e) for e in table['entries'] if e}


def mkfs(sectors, contents=None):
    """A fresh volume. contents: name -> bytes, or name -> (bytes, capacity_sectors)."""
    if sectors < MIN_SECTORS:
        raise FormatError(f'a volume needs at least {MIN_SECTORS} sectors')
    image = bytearray(sectors * SECTOR)
    entries = []
    next_sector = DATA_START
    serial = 1
    for name, value in (contents or {}).items():
        data, capacity = value if isinstance(value, tuple) else (value, 0)
        capacity = max(capacity, -(-len(data) // SECTOR), 1)
        if next_sector + capacity > sectors:
            raise FormatError('volume too small for its files')
        image[next_sector * SECTOR:next_sector * SECTOR + len(data)] = data
        entries.append((name, len(data), next_sector, capacity, serial))
        next_sector += capacity
        serial += 1
    image[0:META_BYTES] = pack_table(1, serial, sectors, entries)
    return bytes(image)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    make = sub.add_parser('mkfs', help='create an image')
    make.add_argument('image', type=Path)
    make.add_argument('--sectors', type=int, required=True)
    make.add_argument('--file', action='append', default=[], metavar='NAME=PATH[:SECTORS]',
                      help='a file to put on the volume, optionally with spare capacity')
    for name in ('ls', 'check'):
        sub.add_parser(name).add_argument('image', type=Path)
    cat = sub.add_parser('cat')
    cat.add_argument('image', type=Path)
    cat.add_argument('name')
    args = parser.parse_args()
    try:
        if args.command == 'mkfs':
            contents = {}
            for spec in args.file:
                name, _, rest = spec.partition('=')
                path, _, capacity = rest.partition(':')
                contents[name] = (Path(path).read_bytes(), int(capacity or 0))
            args.image.write_bytes(mkfs(args.sectors, contents))
            return
        image = args.image.read_bytes()
        slot, table = current(image)
        check_table(table)
        if args.command == 'ls':
            print(f'generation {table["generation"]} (copy {"AB"[slot]}), {table["sectors"]} sectors')
            for e in table['entries']:
                if e:
                    print(f'{e["size"]:10d} {e["capacity"]:6d} {e["first"]:8d}  {e["name"]}')
        elif args.command == 'cat':
            data = files(image).get(args.name)
            if data is None:
                raise FormatError(f'{args.name}: no such file')
            sys.stdout.buffer.write(data)
    except (OSError, FormatError, ValueError) as e:
        parser.exit(1, f'wfs: {e}\n')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Write the producer-neutral approved storage root resource.

The root is the only storage authority the boot image issues. It names a byte
length that starts at the first sector after the boot image. The kernel never
interprets the bytes behind it, so any build step (font packer, filesystem
image, test fixture) may produce it.

Layout, little endian, 16 bytes:
    magic    'WSR1'
    version  1
    bytes    approved length, 1..0x7fffffff
    flags    bit 0: the volume may be written (a multiple of 512 bytes is then
             required); all other bits are reserved and must be zero
"""

import argparse
from pathlib import Path
import struct

MAGIC = b'WSR1'
VERSION = 1
ROOT = struct.Struct('<4s3I')
MAX_BYTES = 0x7FFFFFFF
WRITABLE = 1
SECTOR = 512


def pack(byte_count, writable=False):
    if not 0 < byte_count <= MAX_BYTES:
        raise ValueError(f'storage root must cover 1..{MAX_BYTES} bytes')
    if writable and byte_count % SECTOR:
        raise ValueError('a writable storage root must cover whole 512-byte sectors')
    return ROOT.pack(MAGIC, VERSION, byte_count, WRITABLE if writable else 0)


def unpack(data):
    if len(data) != ROOT.size:
        raise ValueError('storage root must be exactly 16 bytes')
    magic, version, byte_count, flags = ROOT.unpack(data)
    if (magic != MAGIC or version != VERSION or flags & ~WRITABLE or not 0 < byte_count <= MAX_BYTES
            or (flags & WRITABLE and byte_count % SECTOR)):
        raise ValueError('invalid storage root')
    return byte_count


def flags(data):
    unpack(data)
    return ROOT.unpack(data)[3]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('output', type=Path)
    parser.add_argument('--bytes', type=lambda text: int(text, 0), required=True,
                        help='approved byte length after the boot image')
    parser.add_argument('--writable', action='store_true',
                        help='allow the volume to be written (whole sectors only)')
    args = parser.parse_args()
    try:
        args.output.write_bytes(pack(args.bytes, args.writable))
    except (OSError, ValueError) as e:
        parser.exit(1, f'storage_root: {e}\n')


if __name__ == '__main__':
    main()

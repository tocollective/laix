#!/usr/bin/env python3
"""Append a storage volume after the WRMB boot payload, padded to whole sectors.

The volume is the approved storage root: the kernel never interprets it, so it can
be an ELF image, a filesystem image or a test fixture. Prints the padded byte
count, which tools/storage_root.py needs before the kernel is linked.
"""

import argparse
from pathlib import Path
import struct

SECTOR = 512


def padded(volume):
    if not volume:
        raise ValueError('empty volume')
    return volume + bytes(-len(volume) % SECTOR)


def append(image, volume):
    if len(image) < 16:
        raise ValueError('truncated boot header')
    magic, sectors, entry, flags = struct.unpack_from('<4sIII', image)
    if magic != b'WRMB' or not sectors or flags or entry & 3 or entry >= sectors * SECTOR:
        raise ValueError('invalid WRMB boot header')
    if len(image) != sectors * SECTOR:
        raise ValueError('boot payload must match WRMB sector count; a volume may already be appended')
    return image + padded(volume)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('image', type=Path, nargs='?')
    parser.add_argument('volume', type=Path)
    parser.add_argument('--size', action='store_true', help='print the padded volume size and stop')
    args = parser.parse_args()
    try:
        volume = args.volume.read_bytes()
        if args.size:
            print(len(padded(volume)))
            return
        if args.image is None:
            parser.error('image is required unless --size is given')
        args.image.write_bytes(append(args.image.read_bytes(), volume))
    except (OSError, ValueError) as e:
        parser.exit(1, f'append_volume: {e}\n')


if __name__ == '__main__':
    main()

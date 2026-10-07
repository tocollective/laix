#!/usr/bin/env python3
"""The WFS1 volume of the LAIX_CONSOLE=fs acceptance profile.

A small writable volume holding one file. tests/programs/fs/client.m changes it
(create, replace, delete, fill, commit) and tests/probe_fs_cpu.py reads back what
reached the medium. The module is also the single definition of that volume for
the source tests.
"""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfs  # noqa: E402

SECTORS = 40
MOTD = b'Welcome to LA/IX\n'


def pattern(tag, count):
    """The byte sequence the acceptance client writes: (i * 7 + 3 + tag) & 255."""
    return bytes((i * 7 + 3 + tag) & 255 for i in range(count))


def volume():
    return wfs.mkfs(SECTORS, {'motd': MOTD})


# What the volume must hold once the acceptance client has exited with code 0.
FINAL_FILES = {'notes': pattern(2, 700), 'last': pattern(3, 40)}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.write_bytes(volume())
    print(len(volume()))


if __name__ == '__main__':
    main()

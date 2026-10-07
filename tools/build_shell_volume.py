#!/usr/bin/env python3
"""The WFS1 volume of the LAIX_CONSOLE=shell profile.

Holds a message of the day and the three demonstration programs that
user/bin builds, so the shell can list them, read them and have Exec run them.
Free space is left for what the user writes.
"""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfs  # noqa: E402

MOTD = b'Welcome to LA/IX. Type help for commands.\n'
PROGRAMS = ('hello', 'count', 'spin')
SPARE_SECTORS = 64


def volume(programs):
    """programs: name -> ELF bytes."""
    files = {'motd': MOTD, **{name: programs[name] for name in PROGRAMS}}
    used = sum(max(1, -(-len(data) // wfs.SECTOR)) for data in files.values())
    return wfs.mkfs(wfs.DATA_START + used + SPARE_SECTORS, files)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('output', type=Path)
    parser.add_argument('--services', type=Path, required=True, help='directory holding bin-NAME.elf')
    args = parser.parse_args()
    image = volume({name: (args.services / f'bin-{name}.elf').read_bytes() for name in PROGRAMS})
    args.output.write_bytes(image)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Map a session name to the number init runs (user/init/sessions.m).

    sessions.py NAME      print the number
    sessions.py --list    print every name
"""
import argparse
from pathlib import Path
import re
import sys

SOURCE = Path(__file__).resolve().parent.parent / 'user' / 'init' / 'sessions.m'


def sessions():
    """name -> number, from the SESSION_* constants in sessions.m."""
    found = re.findall(r'let SESSION_([A-Z0-9_]+): UWord = (\d+)', SOURCE.read_text())
    return {name.lower(): int(number) for name, number in found}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('name', nargs='?')
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()
    table = sessions()
    if args.list or not args.name:
        print(' '.join(table))
        return
    if args.name not in table:
        sys.exit(f'sessions: unknown session {args.name!r}; known: {", ".join(table)}')
    print(table[args.name])


if __name__ == '__main__':
    main()

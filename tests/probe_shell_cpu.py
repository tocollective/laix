#!/usr/bin/env python3
"""G7 CPU probe: type into the shell and read the machine.

Needs the shell image (LAIX_CONSOLE=shell sh laix/build.sh). The emulator boots
the profile on a private copy of the disk, and an input script types commands at
exact clock ticks (the keyboard FIFO drops keys that arrive before the shell
owns it, and holds 32 events, so the script waits for the slow commands). The
probe reads what the shell, Exec and the programs write to the UART and, after
the machine has been stopped, parses the volume with tools/wfs.py:

* the transcript holds, in order, the listing, the files read back, the output of
  the two programs Exec loaded from the volume and their exit codes;
* the volume ends as the commands left it, whole and committed, and the programs
  on it are unchanged.

Nothing is built. The run is deterministic and takes a few virtual minutes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time

from probe_boot import require
from run_ready import ROOT
from test_kernel import LAIX

sys.path.insert(0, str(LAIX / 'tools'))
import build_shell_volume  # noqa: E402
import wfs  # noqa: E402

CLOCK = 128_000_000
KEY_GAP = 3_000_000                 # ticks between key events (23 ms): the shell echoes each through the console
BOOT_SECONDS = 6                    # before the first key: firmware, kernel, mount, shell start
# (command, seconds the machine needs before the next one is typed)
COMMANDS = [
    ('ls', 4),
    ('cat motd', 3),
    ('write note hello disk', 4),
    ('cat note', 3),
    ('hello 5', 14),
    ('count 3', 14),
    ('cp note copy', 4),
    ('rm note', 4),
    ('ls', 4),
    ('df', 3),
]
# Each regex must match after the previous one's match: the transcript in order.
EXPECTED = [
    r'LA/IX shell\. Type help\.',
    r'\n\s+\d+  motd\n(?:.|\n)*?\s+\d+  hello\n(?:.|\n)*?\s+\d+  count\n(?:.|\n)*?\s+\d+  spin\n',
    r'Welcome to LA/IX\. Type help for commands\.\n',
    r'hello disk\n',
    r'Hello from a loaded program, argument 5\n',
    r'exit 5\n',
    r'  1\n  2\n  3\n',
    r'exit 3\n',
    r'\n\s+\d+  motd\n(?:.|\n)*?  count\n(?:.|\n)*?  spin\n\s+\d+  copy\n',
    r'sectors \d+  free \d+  generation \d+\n',
]
FORBIDDEN = ('PANIC', 'error:', 'fault')
KEYS = None


def hid_table():
    global KEYS
    if KEYS is None:
        from test_shell import HID
        KEYS = HID
    return KEYS


def input_script(commands=COMMANDS, boot=BOOT_SECONDS):
    """The emulator input script text that types the commands."""
    table = hid_table()
    lines = []
    tick = boot * CLOCK
    for command, wait in commands:
        for char in command + '\n':
            usage, shifted = table[char]
            events = ([0xE1] if shifted else []) + [usage]
            for code in events:
                lines.append(f'{tick} key {code} down')
                tick += KEY_GAP
            for code in reversed(events):
                lines.append(f'{tick} key {code} up')
                tick += KEY_GAP
        tick += wait * CLOCK
    return '\n'.join(lines) + '\n', tick


def check_transcript(text):
    position = 0
    for pattern in EXPECTED:
        match = re.compile(pattern).search(text, position)
        require(match, f'transcript lacks {pattern!r} after offset {position}')
        position = match.end()
    for word in FORBIDDEN:
        require(word not in text, f'transcript contains {word!r}')
    return position


def judge_volume(volume, original):
    table = wfs.current(volume)[1]
    wfs.check_table(table)
    files = wfs.files(volume)
    before = wfs.files(original)
    require(set(files) == {'motd', 'hello', 'count', 'spin', 'copy'}, f'unexpected files {sorted(files)}')
    require(files['copy'] == b'hello disk\n', 'the copied file differs')
    for name in ('motd', 'hello', 'count', 'spin'):
        require(files[name] == before[name], f'{name} changed')
    return table['generation']


def run(image, emulator, rom, timeout, log_dir):
    data = image.read_bytes()
    sectors = struct.unpack_from('<I', data, 4)[0]
    boot = sectors * 512
    script, end_tick = input_script()
    with tempfile.TemporaryDirectory(prefix='laix-shell-') as directory:
        disk, keys = Path(directory) / 'boot.img', Path(directory) / 'keys.txt'
        disk.write_bytes(data)
        keys.write_text(script)
        (log_dir / 'shell.input').write_text(script)
        with tempfile.TemporaryFile(mode='w+') as stdout, tempfile.TemporaryFile(mode='w+') as stderr:
            process = subprocess.Popen([str(emulator), '--headless', '--no-net', '--rom', str(rom), '--hdd', str(disk),
                                        '--ram', '4M', '--clock', '128M', '--deterministic', '--input', str(keys)],
                                       cwd=directory, stdout=stdout, stderr=stderr)
            deadline = time.monotonic() + timeout
            text = ''
            try:
                while time.monotonic() < deadline:
                    time.sleep(1)
                    stdout.seek(0)
                    text = stdout.read()
                    if process.poll() is not None:
                        break
                    try:
                        check_transcript(text)
                    except ValueError:
                        continue
                    # Everything expected has appeared; give the last commit a moment.
                    time.sleep(2)
                    stdout.seek(0)
                    text = stdout.read()
                    break
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            stdout.seek(0)
            text = stdout.read()
        final = disk.read_bytes()
    (log_dir / 'shell.uart.txt').write_text(text)
    require(final[:boot] == data[:boot], 'the kernel image on the medium changed')
    consumed = check_transcript(text)
    generation = judge_volume(final[boot:], data[boot:])
    return dict(transcript_bytes=len(text), matched_bytes=consumed, final_generation=generation,
                script_ticks=end_tick, script_seconds=end_tick / CLOCK)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path, help='accepted for the bundle runner; the probe reads no symbols')
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=LAIX / 'build/acceptance/shell')
    parser.add_argument('--timeout', type=float, default=900)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    paths = dict(image=args.image, map=args.map, emulator=args.emulator, rom=args.rom)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    try:
        report = dict(complete=False, sha256=hashes)
        report.update(run(args.image.resolve(), args.emulator.resolve(), args.rom.resolve(), args.timeout, args.log_dir))
        report['complete'] = True
    except (ValueError, OSError, subprocess.TimeoutExpired, wfs.FormatError) as error:
        report = dict(complete=False, error=str(error), sha256=hashes)
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' shell CPU: ' +
          (f"{len(COMMANDS)} commands typed, both programs ran from the volume, final generation {report['final_generation']}"
           if report['complete'] else report['error']))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

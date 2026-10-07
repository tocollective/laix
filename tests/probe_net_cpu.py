#!/usr/bin/env python3
"""G7 CPU probe: the Ethernet broker, driver and IP stack against the emulated network.

Needs the net image (LAIX_CONSOLE=net sh laix/build.sh). The acceptance client
(tests/programs/net/client.m) asks the IP service for its address, pings the
gateway twice, resolves `localhost` and a name that does not exist, and prints
each result to the UART. The probe boots the image twice: with the emulator's
virtual network (the gateway answers ARP and ping, the DNS server answers from
the host's lookups) and with `--no-net` (the link is down, so the client says so
and stops). It reads the UART and checks the lines.

Nothing is built. Both runs are deterministic. The DNS answer for `localhost`
depends on the host's resolver and on the emulator's network policy, so either an
address or "no such name" is accepted; a timeout is not.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time

from probe_boot import require
from run_ready import ROOT
from test_kernel import LAIX

LINK_UP = [
    r'address 10\.0\.2\.15 gateway 10\.0\.2\.2\n',
    r'ping 10\.0\.2\.2 seq 1 ttl \d+\n',
    r'ping 10\.0\.2\.2 seq 2 ttl \d+\n',
    r'resolve localhost: (?:\d+\.\d+\.\d+\.\d+|no such name)\n',
    r'resolve no-such-host\.invalid: (?:no such name|\d+\.\d+\.\d+\.\d+)\n',
    r'network ok\n',
]
LINK_DOWN = [
    r'address 10\.0\.2\.15 gateway 10\.0\.2\.2\n',
    r'link down\n',
]
FORBIDDEN = ('PANIC', 'failed', 'fault')


def check(text, patterns, name):
    position = 0
    for pattern in patterns:
        match = re.compile(pattern).search(text, position)
        require(match, f'{name}: the UART lacks {pattern!r} after offset {position}')
        position = match.end()
    for word in FORBIDDEN:
        require(word not in text, f'{name}: the UART contains {word!r}')
    return position


def boot(image, emulator, rom, timeout, patterns, name, net):
    with tempfile.TemporaryDirectory(prefix='laix-net-') as directory:
        disk = Path(directory) / 'boot.img'
        disk.write_bytes(image.read_bytes())
        arguments = [str(emulator), '--headless', '--rom', str(rom), '--hdd', str(disk),
                     '--ram', '2M', '--clock', '128M', '--deterministic']
        if not net:
            arguments.append('--no-net')
        with tempfile.TemporaryFile(mode='w+') as stdout, tempfile.TemporaryFile(mode='w+') as stderr:
            process = subprocess.Popen(arguments, cwd=directory, stdout=stdout, stderr=stderr)
            deadline = time.monotonic() + timeout
            text = ''
            try:
                while time.monotonic() < deadline:
                    time.sleep(1)
                    stdout.seek(0)
                    text = stdout.read()
                    try:
                        check(text, patterns, name)
                        break
                    except ValueError:
                        if process.poll() is not None:
                            break
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            stdout.seek(0)
            text = stdout.read()
    check(text, patterns, name)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('map', type=Path, help='accepted for the bundle runner; the probe reads no symbols')
    parser.add_argument('--emulator', type=Path, default=ROOT / 'bin/wrm081632')
    parser.add_argument('--rom', type=Path, default=ROOT / 'bin/firmware.rom')
    parser.add_argument('--log-dir', type=Path, default=LAIX / 'build/acceptance/net')
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    paths = dict(image=args.image, map=args.map, emulator=args.emulator, rom=args.rom)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    report = dict(complete=False, sha256=hashes)
    try:
        for name, patterns, net in (('link-up', LINK_UP, True), ('link-down', LINK_DOWN, False)):
            text = boot(args.image.resolve(), args.emulator.resolve(), args.rom.resolve(), args.timeout, patterns, name, net)
            (args.log_dir / f'{name}.uart.txt').write_text(text)
            report[name] = text.splitlines()
        require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()),
                'probe changed input artifacts')
        report['complete'] = True
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report['error'] = str(error)
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + ' network CPU: ' +
          ('gateway pinged twice, names resolved, link-down run stopped cleanly' if report['complete'] else report['error']))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

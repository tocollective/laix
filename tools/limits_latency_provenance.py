#!/usr/bin/env python3
"""Record and verify A8 source, image and CPU evidence without building WRM."""
import argparse
import hashlib
import json
from pathlib import Path

LAIX = Path(__file__).resolve().parents[1]
ROOT = LAIX.parent
OUTPUT = LAIX / 'tests/LIMITS_LATENCY_PROVENANCE.json'


def entry(path):
    return dict(path=str(path.relative_to(ROOT)), bytes=path.stat().st_size,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.verify:
        record = json.loads(OUTPUT.read_text())
        for item in record['sources'] + record['artifacts']:
            if entry(ROOT / item['path']) != item:
                raise SystemExit('provenance mismatch: ' + item['path'])
        print('PASS A8 source/artifact provenance')
        return
    results = {}
    reports = []
    for name in ('limits-latency', 'limits-latency-device', 'limits-latency-ipc', 'limits-latency-liveness'):
        path = LAIX / 'build/acceptance' / name / 'results.json'
        report = json.loads(path.read_text())
        if not report.get('complete'):
            raise SystemExit('incomplete CPU acceptance: ' + name)
        results[name] = report
        reports.append(path)
    artifacts = LAIX / 'build/acceptance/limits-latency/artifacts'
    bound = [(artifacts / 'latency.img', results['limits-latency']['sha256']['image']),
             (artifacts / 'latency.map', results['limits-latency']['sha256']['map']),
             (artifacts / 'services.img', results['limits-latency-device']['sha256']['image']),
             (artifacts / 'services.map', results['limits-latency-device']['sha256']['map'])]
    for path, expected in bound:
        if entry(path)['sha256'] != expected:
            raise SystemExit('CPU/image binding mismatch: ' + str(path))
    sources = set()
    for directory in (LAIX / 'src', LAIX / 'user', LAIX / 'tests/programs'):
        for path in directory.rglob('*'):
            if path.is_file() and path.suffix in ('.m', '.asm', '.inc', '.py') and 'build' not in path.parts:
                sources.add(path)
    sources.update(path for path in (ROOT / 'mc').rglob('*.py') if '__pycache__' not in path.parts)
    sources.update(path for path in (ROOT / 'mc/runtime').rglob('*') if path.is_file())
    for path in LAIX.glob('tests/*.py'):
        sources.add(path)
    sources.update((LAIX / 'build.sh', LAIX / 'tools/build_services.sh', Path(__file__).resolve()))
    inputs = [path for path, _ in bound] + reports + [ROOT / 'bin/wrm081632',
              LAIX / 'build/acceptance/screen-firmware.rom', LAIX / 'build/laix.img', LAIX / 'build/laix.map',
              LAIX / 'fonts/unifont-index.laf', LAIX / 'fonts/storage-extent.bin',
              LAIX / 'fonts/unifont-console.laf', LAIX / 'build/acceptance/limits-latency/source-suite.txt',
              LAIX / 'build/acceptance/limits-latency/source-a8.txt']
    inputs.extend(sorted((LAIX / 'build/services').glob('*.elf')))
    for name in ('limits-latency', 'limits-latency-device', 'limits-latency-ipc', 'limits-latency-liveness'):
        inputs.extend(sorted((LAIX / 'build/acceptance' / name).glob('*.monitor.txt')))
        inputs.extend(sorted((LAIX / 'build/acceptance' / name).glob('*.uart.txt')))
    emulator_hash = entry(ROOT / 'bin/wrm081632')['sha256']
    rom_hash = entry(LAIX / 'build/acceptance/screen-firmware.rom')['sha256']
    for report in results.values():
        hashes = report.get('sha256') or {name: item['sha256'] for name, item in report['artifacts'].items()}
        if hashes['emulator'] != emulator_hash or hashes['rom'] != rom_hash:
            raise SystemExit('CPU reports use different WRM/ROM bytes')
        for item in report.get('artifacts', {}).values():
            if hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() != item['sha256']:
                raise SystemExit('CPU input binding changed: ' + item['path'])
    record = dict(date='2026-10-05', wrm_built=False,
                  source_evidence=dict(full_suite_tests=389, full_suite_seconds=530.308,
                                       final_a8_tests=8, note='Full suite precedes two additional A8 generation-boundary tests; all eight A8 tests pass afterward.'),
                  sources=[entry(path) for path in sorted(sources)],
                  artifacts=[entry(path) for path in sorted(set(inputs))], cpu_results=results,
                  scope='Kernel/user/compiler source snapshot; immutable CPU image/map/WRM/ROM bindings. AST fixtures do not measure timing. No cached ASIDs, staged teardown, priorities or nested traps.')
    OUTPUT.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
    print('Recorded A8 provenance')


if __name__ == '__main__':
    main()

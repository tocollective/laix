#!/usr/bin/env python3
"""Package, verify and run identified CPU inputs. This tool never builds code."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ('uart', 'screen', 'services', 'uart-stress', 'screen-stress',
            'memory', 'sharing', 'objects', 'supervisor', 'soak', 'loader', 'fs', 'shell', 'net', 'recovery', 'recovery-production', 'lifetime',
            'screenrecovery', 'screenrecovery-watchdog', 'screenrecovery-production',
            'latency', 'hid', 'media')
SCHEMA = 1
# Profiles whose image is not named after them.
IMAGES = {'uart': 'laix', 'uart-stress': 'laix', 'screen-stress': 'laix', 'hid': 'laix', 'latency': 'laix',
          'media': 'services', 'recovery-production': 'recovery', 'lifetime': 'recovery',
          'screenrecovery-watchdog': 'screenrecovery', 'screenrecovery-production': 'screenrecovery'}
# Build-time provenance records the probes compare with the current sources.
PROVENANCE = {'recovery': 'recovery', 'recovery-production': 'recovery', 'lifetime': 'recovery',
              'screenrecovery': 'screenrecovery', 'screenrecovery-watchdog': 'screenrecovery',
              'screenrecovery-production': 'screenrecovery'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_state():
    paths = set()
    for tree in ('laix/src', 'laix/user', 'laix/tests', 'laix/tools', 'mc/mlang', 'mc/runtime', 'include', 'source', 'wfw/src'):
        for path in (ROOT / tree).rglob('*'):
            if path.is_file() and path.suffix in ('.m', '.asm', '.inc', '.py', '.sh', '.h', '.c', '.hex'):
                paths.add(path)
    paths.update((ROOT / 'mc').glob('*.py'))
    paths.add(ROOT / 'laix/build.sh')
    paths.add(ROOT / 'vendor/SDL/test/unifont-15.1.05.hex')
    paths.update((ROOT / '.github/workflows').glob('*.yml'))
    revisions = {}
    for name in ('.', 'laix', 'mc', 'wfw', 'vendor/SDL'):
        result = subprocess.run(['git', '-C', str(ROOT / name), 'rev-parse', 'HEAD'], capture_output=True, text=True)
        revisions[name] = result.stdout.strip() if result.returncode == 0 else None
    # A content manifest binds dirty worktrees as well as committed revisions.
    return dict(revisions=revisions, files={str(path.relative_to(ROOT)): digest(path) for path in sorted(paths)})


def safe_file(base, name):
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('unsafe manifest path: ' + name)
    target = base / path
    if not target.resolve().is_relative_to(base.resolve()) or target.is_symlink() or not target.is_file():
        raise ValueError('missing or unsafe artifact: ' + name)
    return target


def verify(bundle, profile=None, match_source=True):
    record = json.loads((bundle / 'manifest.json').read_text())
    if record.get('schema') != SCHEMA or record.get('profile') not in PROFILES:
        raise ValueError('unsupported bundle schema/profile')
    if profile is not None and record['profile'] != profile:
        raise ValueError('bundle profile mismatch')
    if match_source and record['source'] != source_state():
        raise ValueError('build source/compiler/probe manifest does not match checkout')
    files = record['files']
    image = IMAGES.get(record['profile'], record['profile'])
    required = {'tools/wrm081632', 'tools/firmware.rom',
                f'laix/build/{image}.img', f'laix/build/{image}.map',
                'laix/fonts/unifont-console.laf', 'laix/fonts/unifont-index.laf', 'laix/fonts/storage-extent.bin'}
    if not required <= files.keys():
        raise ValueError('required image/map/tool/resource artifacts are missing')
    for name in files:
        if not (name.startswith('laix/build/') or name.startswith('laix/fonts/') or name in ('tools/wrm081632', 'tools/firmware.rom')):
            raise ValueError('unexpected artifact destination: ' + name)
    for name, item in files.items():
        path = safe_file(bundle, name)
        if path.stat().st_size != item['bytes'] or digest(path) != item['sha256']:
            raise ValueError('artifact hash mismatch: ' + name)
    return record


def pack(args):
    if args.dest.exists():
        raise ValueError('bundle destination must be new; preserve older run inputs')
    source = source_state()
    args.dest.mkdir(parents=True)
    files = {}
    def copy(path, name):
        target = args.dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files[name] = dict(bytes=target.stat().st_size, sha256=digest(target))
    copy(args.emulator, 'tools/wrm081632')
    copy(args.rom, 'tools/firmware.rom')
    image_name = IMAGES.get(args.profile, args.profile)
    for suffix in ('img', 'map'):
        copy(ROOT / f'laix/build/{image_name}.{suffix}', f'laix/build/{image_name}.{suffix}')
    # Keep maps and ELFs together; probes decode executable instructions/ABI.
    directories = {
        'screen': ('services',), 'services': ('services',), 'media': ('services',),
        'hid': ('services',), 'uart-stress': ('services',), 'screen-stress': ('services',),
        'loader': ('services',), 'fs': ('services',), 'shell': ('services',), 'net': ('services',),
        'memory': ('memory-user',), 'sharing': ('sharing-user',), 'objects': ('objects-user',),
        'recovery': ('recovery-user',), 'recovery-production': ('recovery-user',), 'lifetime': ('recovery-user',),
        'screenrecovery': ('screen-recovery-user',), 'screenrecovery-watchdog': ('screen-recovery-user',),
        'screenrecovery-production': ('screen-recovery-user',),
    }.get(args.profile, ())
    service_names = {
        'screen': ('screen', 'storage', 'application'),
        'services': ('input', 'disk', 'files', 'simple-application'),
        'media': ('input', 'disk', 'files', 'simple-application'),
        'hid': ('input', 'disk', 'files', 'simple-application'),
        'uart-stress': ('screen', 'storage', 'application', 'stress-client'),
        'screen-stress': ('screen', 'storage', 'application', 'stress-client'),
        'loader': ('input', 'disk', 'files', 'loader', 'hello'),
        'fs': ('input', 'disk', 'fs', 'fsclient'),
        'shell': ('disk', 'fs', 'exec', 'shell', 'bin-hello', 'bin-count', 'bin-spin'),
        'net': ('netdrv', 'ip', 'netclient'),
    }
    for directory in directories:
        for path in sorted((ROOT / 'laix/build' / directory).glob('*')):
            if path.suffix in ('.elf', '.map') and (directory != 'services' or path.stem in service_names[args.profile]):
                copy(path, str(path.relative_to(ROOT)))
    for path in sorted((ROOT / 'laix/fonts').glob('*.laf')):
        copy(path, str(path.relative_to(ROOT)))
    copy(ROOT / 'laix/fonts/storage-extent.bin', 'laix/fonts/storage-extent.bin')
    if args.profile in PROVENANCE:
        record_name = f'laix/build/{PROVENANCE[args.profile]}.provenance.json'
        copy(ROOT / record_name, record_name)
    if source != source_state():
        raise ValueError('sources changed while packaging')
    record = dict(schema=SCHEMA, profile=args.profile, source=source, files=files,
                  wrm_built=False, emulator_source_binding='approved bytes; current WRM source is recorded, not certified',
                  producer=dict(run_id=args.producer_run_id, source_binding='built from this source manifest'))
    (args.dest / 'manifest.json').write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
    verify(args.dest, args.profile)
    print('PASS packaged ' + args.profile)


def commands(profile, emulator, rom, logs):
    build = ROOT / 'laix/build'
    image = IMAGES.get(profile, profile)
    common = [str(build / (image + '.img')), str(build / (image + '.map')),
              '--emulator', str(emulator), '--rom', str(rom)]
    suites = {
        'uart': [('probe_bootstrap_cpu.py', []), ('probe_ipc_request_reply_cpu.py', []),
                 ('probe_ipc_liveness_cpu.py', []), ('probe_raw_transport_cpu.py', ['--rounds', '128'])],
        'screen': [('probe_screen_cpu.py', []), ('probe_service_panic_cpu.py', []),
                   ('probe_device_events_cpu.py', ['--case', 'rearm-boundary', '--case', 'wait-boundary'])],
        'services': [('probe_simple_services_cpu.py', []), ('probe_device_latency_cpu.py', [])],
        'uart-stress': [('probe_multiclient_cpu.py', ['--profile', 'uart'])],
        'screen-stress': [('probe_multiclient_cpu.py', ['--profile', 'screen'])],
        'memory': [('probe_runtime_memory_cpu.py', [])],
        'sharing': [('probe_memory_sharing_cpu.py', [])],
        'objects': [('probe_runtime_objects_cpu.py', [])],
        'supervisor': [('probe_runtime_tasks_cpu.py', [])],
        'soak': [('probe_soak_cpu.py', [])],
        'loader': [('probe_loader_cpu.py', [])],
        'fs': [('probe_fs_cpu.py', [])],
        'shell': [('probe_shell_cpu.py', [])],
        'net': [('probe_net_cpu.py', [])],
        'recovery': [('probe_service_recovery_cpu.py', [])],
        'recovery-production': [('probe_service_recovery_cpu.py', ['--production', '--window', '512', '96'])],
        'lifetime': [('probe_lifetime_cpu.py', [])],
        'screenrecovery': [('probe_screen_recovery_cpu.py', [])],
        'screenrecovery-watchdog': [('probe_screen_recovery_cpu.py', ['--watchdog'])],
        'screenrecovery-production': [('probe_screen_recovery_cpu.py', ['--production'])],
        # The same image at the largest installed RAM (4 x 32M); a third element
        # names a suite's logs when a probe runs more than once.
        'latency': [('probe_limits_latency_cpu.py', ['--rounds', '4']),
                    ('probe_limits_latency_cpu.py', ['--rounds', '4', '--ram', '32M,32M,32M,32M'], 'ram128m')],
        'hid': [('probe_hid_cpu.py', [])],
        'media': [('probe_device_events_cpu.py', ['--case', 'media-removal', '--case', 'media-replacement'])],
    }
    rows = []
    for name, extra, *tag in suites[profile]:
        label = name if not tag else name.removesuffix('.py') + '-' + tag[0] + '.py'
        rows.append((label, [sys.executable, '-B', str(Path(__file__).parent / name), *common,
                             '--log-dir', str(logs / label.removesuffix('.py')), *extra]))
    return rows


def run(args):
    args.log_dir.mkdir(parents=True, exist_ok=True)
    report = dict(complete=False, profile=args.profile, results=[], probe_schema=SCHEMA)
    try:
        record = verify(args.bundle, args.profile)
        report['inputs'] = record
        report['bundle_manifest_sha256'] = digest(args.bundle / 'manifest.json')
        # Existing probes use repository-relative ancillary paths. Restore only
        # their verified generated artifacts; checkout source files are untouched.
        for name in record['files']:
            if name.startswith('laix/build/') or name.startswith('laix/fonts/'):
                target = ROOT / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(safe_file(args.bundle, name), target)
        emulator = safe_file(args.bundle, 'tools/wrm081632').resolve()
        emulator.chmod(emulator.stat().st_mode | 0o100)
        rom = safe_file(args.bundle, 'tools/firmware.rom').resolve()
        for name, command in commands(args.profile, emulator, rom, args.log_dir):
            started = time.monotonic()
            with (args.log_dir / (name + '.txt')).open('w') as output:
                try:
                    result = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                            timeout=args.timeout)
                    row = dict(probe=name, exit_code=result.returncode, passed=result.returncode == 0)
                except subprocess.TimeoutExpired:
                    row = dict(probe=name, passed=False, error='suite process timeout')
            row.update(command=command, seconds=time.monotonic() - started)
            report['results'].append(row)
            print(('PASS ' if row['passed'] else 'FAIL ') + name, flush=True)
        verify(args.bundle, args.profile)
        for name, item in record['files'].items():
            if name.startswith('laix/') and digest(ROOT / name) != item['sha256']:
                raise ValueError('probe changed restored artifact: ' + name)
        report['complete'] = bool(report['results']) and all(row['passed'] for row in report['results'])
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        report['error'] = str(error)
        print('FAIL acceptance: ' + str(error), flush=True)
    (args.log_dir / 'results.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    return 0 if report['complete'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='action', required=True)
    package = subs.add_parser('pack')
    package.add_argument('--profile', choices=PROFILES, required=True)
    package.add_argument('--dest', type=Path, required=True)
    package.add_argument('--emulator', type=Path, required=True)
    package.add_argument('--rom', type=Path, required=True)
    package.add_argument('--producer-run-id', default='local')
    package.add_argument('--built-from-current-source', action='store_true', required=True,
                         help='assert LA/IX inputs were built from the unchanged recorded checkout')
    check = subs.add_parser('verify')
    check.add_argument('bundle', type=Path)
    execute = subs.add_parser('run')
    execute.add_argument('--bundle', type=Path, required=True)
    execute.add_argument('--profile', choices=PROFILES, required=True)
    execute.add_argument('--log-dir', type=Path, required=True)
    execute.add_argument('--timeout', type=float, default=1800)
    args = parser.parse_args()
    if args.action == 'run':
        return run(args)
    try:
        if args.action == 'pack':
            pack(args)
        else:
            verify(args.bundle)
            print('PASS bundle/source provenance')
        return 0
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        print('FAIL bundle: ' + str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Record A6 source, toolchain and CPU acceptance artifact provenance."""
import hashlib
import json
from pathlib import Path
import subprocess

from recovery_provenance import ROOT, LAIX, digest, source_manifest


def manifest():
    sources = source_manifest()
    for name in ('tools/build_runtime_objects.sh', 'tools/capability_transfer_provenance.py'):
        path = LAIX / name
        sources[str(path.relative_to(ROOT))] = digest(path)
    font = ROOT / 'vendor/SDL/test/unifont-15.1.05.hex'
    sources[str(font.relative_to(ROOT))] = digest(font)
    sources = dict(sorted(sources.items()))
    paths = [LAIX / f'build/{name}.{suffix}' for name in ('objects', 'supervisor')
             for suffix in ('img', 'map')]
    paths += [LAIX / f'build/objects-user/objects.{suffix}' for suffix in ('elf', 'map')]
    paths += [ROOT / 'bin/wrm081632', ROOT / 'bin/firmware.rom']
    paths += sorted((LAIX / 'build/acceptance/capability-transfer').rglob('*'))
    if any(not path.is_file() for path in paths[:8]):
        raise ValueError('required image/map/emulator/ROM artifact missing')
    artifacts = {str(path.relative_to(ROOT)): digest(path) for path in paths if path.is_file()}
    results = json.loads((LAIX / 'build/acceptance/capability-transfer/results.json').read_text())
    supervisor = json.loads((LAIX / 'build/acceptance/capability-transfer/supervisor/results.json').read_text())
    if not supervisor.get('complete'):
        raise ValueError('supervisor CPU acceptance is not complete')
    source_log = (LAIX / 'build/acceptance/capability-transfer/source-tests.txt').read_text()
    if not source_log.rstrip().endswith('OK'):
        raise ValueError('full source acceptance is not complete')
    if not results.get('complete'):
        raise ValueError('A6 CPU acceptance is not complete')
    return dict(date='2026-10-05',
                base_commits={name: subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'],
                                                           text=True).strip()
                              for name, path in (('wrm', ROOT), ('laix', LAIX), ('mc', ROOT / 'mc'))},
                source_manifest_sha256=hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
                sources=sources, artifacts=artifacts, cpu_acceptance=results,
                wrm_source_build=False)


if __name__ == '__main__':
    output = LAIX / 'tests/CAPABILITY_TRANSFER_PROVENANCE.json'
    output.write_text(json.dumps(manifest(), indent=2, sort_keys=True) + '\n')
    print(output)

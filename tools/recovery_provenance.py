#!/usr/bin/env python3
"""Bind recovery artifacts to all compiler, kernel and user input sources."""
import hashlib
import json
import os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
LAIX = ROOT / 'laix'

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def source_manifest():
    paths = set()
    for tree in ('laix/src', 'laix/user', 'laix/tests', 'mc/mlang', 'mc/runtime'):
        for path in (ROOT / tree).rglob('*'):
            if path.is_file() and path.suffix in ('.m', '.asm', '.inc', '.py'):
                paths.add(path)
    paths.update((ROOT / 'mc').glob('*.py'))
    paths.update([LAIX/'build.sh', LAIX/'tools/build_recovery.sh', Path(__file__).resolve(),
                  LAIX/'tools/append_font.py', LAIX/'tools/pack_unifont.py', LAIX/'fonts/unifont-index.laf',
                  LAIX/'fonts/unifont-console.laf', LAIX/'fonts/storage-extent.bin'])
    return {str(path.relative_to(ROOT)): digest(path) for path in sorted(paths)}

def artifacts():
    paths = [LAIX/'build/recovery.img', LAIX/'build/recovery.map']
    paths += [LAIX/f'build/recovery-user/{name}.{suffix}'
              for name in ('echo','disk','files','policy') for suffix in ('elf','map')]
    return {str(path.relative_to(ROOT)): digest(path) for path in paths}

def fixtures():
    """True for the service-fault fixtures, False for production, "lifetime" for
    the G4 scenario. Probes test with `is True` / `is False`, so the string
    matches neither of the older profiles."""
    mode = os.environ.get("LAIX_RECOVERY_FIXTURES", "0")
    return "lifetime" if mode == "lifetime" else mode == "1"

if __name__ == '__main__':
    (LAIX/'build/recovery.provenance.json').write_text(json.dumps(
        dict(fixtures=fixtures(), sources=source_manifest(), artifacts=artifacts()), indent=2, sort_keys=True)+'\n')

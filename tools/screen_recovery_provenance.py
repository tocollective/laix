#!/usr/bin/env python3
"""Bind supervised-display artifacts to all compiler, kernel and user input sources."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from recovery_provenance import LAIX, digest, source_manifest as recovery_manifest

def source_manifest():
    manifest = recovery_manifest()
    for path in (LAIX/'tools/build_screen_recovery.sh', Path(__file__).resolve()):
        manifest[str(path.relative_to(LAIX.parent))] = digest(path)
    return dict(sorted(manifest.items()))

def artifacts():
    paths = [LAIX/'build/screenrecovery.img', LAIX/'build/screenrecovery.map']
    paths += [LAIX/f'build/screen-recovery-user/{name}.{suffix}'
              for name in ('echo', 'bitmap', 'screen', 'policy') for suffix in ('elf', 'map')]
    return {str(path.relative_to(LAIX.parent)): digest(path) for path in paths}

def fixture_mode():
    """'' (production), 'scenario' (scripted crash supervisor) or 'watchdog' (production supervisor)."""
    mode = os.environ.get("LAIX_SCREEN_RECOVERY_FIXTURES", "0")
    return {"0": "", "1": "scenario"}.get(mode, mode)

if __name__ == '__main__':
    (LAIX/'build/screenrecovery.provenance.json').write_text(json.dumps(
        dict(fixtures=fixture_mode(),
             sources=source_manifest(), artifacts=artifacts()), indent=2, sort_keys=True)+'\n')

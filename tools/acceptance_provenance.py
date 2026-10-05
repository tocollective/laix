#!/usr/bin/env python3
"""Record one completed A9 campaign; never rewrite historical acceptance hashes."""
import argparse
import json
from pathlib import Path
import sys

LAIX = Path(__file__).resolve().parents[1]
ROOT = LAIX.parent
sys.path.insert(0, str(LAIX / 'tests'))
from acceptance_bundle import PROFILES, digest, source_state, verify

OUTPUT = LAIX / 'tests/ACCEPTANCE_CI_PROVENANCE.json'


def entry(path):
    return dict(path=str(path.relative_to(ROOT)), bytes=path.stat().st_size, sha256=digest(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', type=Path, default=LAIX / 'build/acceptance/a9/complete')
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    try:
        if args.verify:
            record = json.loads(OUTPUT.read_text())
            if record['source'] != source_state():
                raise ValueError('current sources differ from the accepted run')
            for item in record['artifacts']:
                if entry(ROOT / item['path']) != item:
                    raise ValueError('accepted artifact changed: ' + item['path'])
            print('PASS A9 campaign source/artifact provenance')
            return 0
        base = args.campaign.resolve()
        source = source_state()
        accepted = json.loads((base / 'source/source-results.json').read_text())
        if not accepted['complete'] or accepted['source'] != source:
            raise ValueError('complete matching source acceptance required')
        profiles = {}
        paths = set((base / 'source').rglob('*'))
        for profile in PROFILES:
            bundle = base / 'inputs' / profile
            manifest = verify(bundle, profile)
            result_path = base / 'cpu' / profile / 'results.json'
            report = json.loads(result_path.read_text())
            if not report['complete'] or report['inputs'] != manifest or report['bundle_manifest_sha256'] != digest(bundle / 'manifest.json'):
                raise ValueError('CPU/bundle/source binding mismatch: ' + profile)
            profiles[profile] = dict(complete=True, probes=report['results'], files=manifest['files'],
                                     manifest=entry(bundle / 'manifest.json'), report=entry(result_path))
            paths.update((base / 'cpu' / profile).rglob('*'))
            paths.update(bundle.rglob('*'))
        record = dict(date='2026-10-05', schema=1, wrm_built=False, rom_built=False,
                      source=source, source_tests=accepted['tests'], source_seconds=accepted['seconds'],
                      profiles=profiles, artifacts=[entry(path) for path in sorted(paths) if path.is_file()],
                      scope='Current identified inputs only. Separate source, CPU and bounded sustained-stress evidence; no universal WCET or automatic service restart claim.')
        OUTPUT.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
        print('PASS recorded A9 acceptance provenance')
        return 0
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        print('FAIL A9 provenance: ' + str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the complete source suite with deterministic discovery and JSON failures."""
import argparse
import json
from pathlib import Path
import sys
import time
import traceback
import unittest


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def record(self, test, outcome, error=None):
        row = dict(test=test.id(), outcome=outcome)
        if error is not None:
            row['traceback'] = ''.join(traceback.format_exception(*error)) if isinstance(error, tuple) else str(error)
        self.records.append(row)

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, 'passed')

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, 'failed', err)

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, 'error', err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, 'skipped', reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.record(test, 'expected-failure', err)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.record(test, 'unexpected-success')

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(subtest, 'failed', err)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log-dir', type=Path, required=True)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    from acceptance_bundle import source_state
    before = source_state()
    started = time.monotonic()
    report = dict(complete=False, source=before, python=sys.version, results=[])
    try:
        suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern='test_*.py')
        with (args.log_dir / 'source-suite.txt').open('w') as log:
            result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=Result).run(suite)
        report.update(complete=result.wasSuccessful() and result.testsRun > 0,
                      tests=result.testsRun, results=result.records)
        if before != source_state():
            report.update(complete=False, error='source inputs changed during suite')
    except Exception:
        report['error'] = traceback.format_exc()
    report['seconds'] = time.monotonic() - started
    (args.log_dir / 'source-results.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(('PASS' if report['complete'] else 'FAIL') + f" LA/IX source suite: {report.get('tests', 0)} tests; logs: {args.log_dir}")
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

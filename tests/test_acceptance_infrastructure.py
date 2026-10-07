"""Fail-closed artifact verification and deterministic source failure reporting."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import acceptance_bundle as bundle
from run_source_suite import Result
from test_kernel import LAIX, check_m


class AcceptanceInfrastructureTests(unittest.TestCase):
    def fixture(self, root):
        paths = ['tools/wrm081632', 'tools/firmware.rom', 'laix/build/laix.img', 'laix/build/laix.map',
                 'laix/fonts/unifont-console.laf', 'laix/fonts/unifont-index.laf', 'laix/fonts/storage-extent.bin']
        record = dict(schema=1, profile='uart', source={'revision': 'fixture'}, files={})
        for name in paths:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
            record['files'][name] = dict(bytes=path.stat().st_size, sha256=bundle.digest(path))
        (root / 'manifest.json').write_text(json.dumps(record))
        return record

    def test_modified_tool_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / 'tools/wrm081632').write_bytes(b'changed')
            with patch.object(bundle, 'source_state', return_value={'revision': 'fixture'}), \
                 patch.object(bundle.subprocess, 'run') as execute:
                with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                    bundle.verify(root)
                execute.assert_not_called()

    def test_missing_map_and_source_mismatch_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self.fixture(root)
            with patch.object(bundle, 'source_state', return_value={'revision': 'different'}):
                with self.assertRaisesRegex(ValueError, 'manifest does not match'):
                    bundle.verify(root)
            del record['files']['laix/build/laix.map']
            (root / 'manifest.json').write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, 'artifacts are missing'):
                bundle.verify(root, match_source=False)

    def test_traversal_and_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'file'
            target.write_text('fixture')
            (root / 'link').symlink_to(target)
            for name in ('../file', str(target), 'link'):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'unsafe'):
                    bundle.safe_file(root, name)

    def test_wrong_profile_cannot_certify_other_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            with self.assertRaisesRegex(ValueError, 'profile mismatch'):
                bundle.verify(root, 'screen', match_source=False)

    def test_subtest_failures_and_import_errors_have_stable_ids(self):
        class Failing(unittest.TestCase):
            def runTest(self):
                with self.subTest(boundary='generation'):
                    self.assertEqual(1, 2)
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, resultclass=Result).run(unittest.TestSuite([Failing()]))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(result.records[0]['outcome'], 'failed')
        self.assertIn('generation', result.records[0]['test'])
        self.assertIn('AssertionError', result.records[0]['traceback'])
        loader = unittest.TestLoader()
        failed_import = loader.loadTestsFromName('laix_nonexistent_acceptance_fixture')
        result = unittest.TextTestRunner(stream=output, resultclass=Result).run(failed_import)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(result.records[0]['outcome'], 'error')

    def test_all_acceptance_mains_typecheck_and_stress_clients_use_timer(self):
        for name in ('uart_stress', 'screen_stress', 'input_stress'):
            check_m(LAIX / f'tests/programs/console/{name}.m')
        for name in ('stress_client', 'input_client'):
            modules = check_m(LAIX / f'tests/programs/console/{name}.m')
            self.assertTrue(any('sleep' in module.scope for module in modules))

    def test_unrelated_compiler_examples_do_not_invalidate_build_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('mc/mc.py', 'mc/mlang/parser.py', 'mc/runtime/mem.asm',
                         'mc/examples/demo.m', 'laix/build.sh', 'vendor/SDL/test/unifont-15.1.05.hex'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture')
            git = subprocess.CompletedProcess([], 0, stdout='revision', stderr='')
            with patch.object(bundle, 'ROOT', root), patch.object(bundle.subprocess, 'run', return_value=git):
                before = bundle.source_state()
                (root / 'mc/examples/demo.m').write_text('edited unrelated application')
                self.assertEqual(bundle.source_state(), before)
                (root / 'mc/mlang/parser.py').write_text('changed compiler')
                self.assertNotEqual(bundle.source_state(), before)

    def test_each_supported_profile_resolves_to_existing_no_build_probes(self):
        for profile in bundle.PROFILES:
            commands = bundle.commands(profile, Path('/existing/emulator'), Path('/existing/rom'), Path('/logs'))
            self.assertTrue(commands, profile)
            self.assertEqual(len({name for name, _ in commands}), len(commands), profile)
            for name, command in commands:
                self.assertTrue(Path(command[2]).is_file(), command[2])
                self.assertNotIn('build.sh', ' '.join(command))
                self.assertIn('--log-dir', command)


if __name__ == '__main__':
    unittest.main()

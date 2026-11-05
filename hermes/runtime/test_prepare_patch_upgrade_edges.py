"""Edge-case coverage for managed patch retirement before an upgrade."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import runpy
import sys
import unittest
from unittest import mock

from hermes.runtime import test_installer_pin


class PatchUpgradeEdgeCaseTests(unittest.TestCase):
    source = test_installer_pin.FIXTURE
    setUp = test_installer_pin.InstallerPinTests.setUp
    git = test_installer_pin.InstallerPinTests.git

    def prepare_managed(self):
        return runpy.run_path(str(Path(__file__).with_name('prepare-patch-upgrade.py')))

    def write_records(self, records):
        (self.repo / '.local-hermes-patches.json').write_text(json.dumps(records))

    def managed_edit(self):
        original = (self.repo / 'gateway.py').read_text()
        block = '# Local Hermes: fixture\nmanaged value\n'
        current = original.replace('default\n', '\n' + block)
        (self.repo / 'gateway.py').write_text(current)
        self.write_records({'# Local Hermes: fixture': {
            'source': block, 'digest': hashlib.sha256(block.encode()).hexdigest()}})
        return original, current

    def test_non_mapping_record_entries_are_ignored(self):
        retire = self.prepare_managed()['retire']
        current = 'before\n# Local Hermes: fixture\nmanaged value\nafter\n'
        self.assertEqual(retire('before\nafter\n', current, {'# Local Hermes: fixture': 'not-a-record'}), current)

    def test_record_without_trailing_newline_takes_the_separator_with_it(self):
        retire = self.prepare_managed()['retire']
        block = '# Local Hermes: tail\nmanaged value'
        records = {'# Local Hermes: tail': {
            'source': block, 'digest': hashlib.sha256(block.encode()).hexdigest()}}
        self.assertEqual(retire('before\nafter\n', 'before\n' + block + '\nafter\n', records), 'before\nafter\n')

    def test_prepare_without_state_file_is_a_noop(self):
        self.managed_edit()
        (self.repo / '.local-hermes-patches.json').unlink()
        self.assertIsNone(self.prepare_managed()['prepare'](self.repo))

    def test_state_file_must_contain_a_mapping(self):
        self.managed_edit()
        self.write_records(['# Local Hermes: fixture'])
        prepare = self.prepare_managed()['prepare']
        with self.assertRaisesRegex(RuntimeError, 'Invalid managed patch state'):
            prepare(self.repo)

    def test_deleted_and_symlinked_targets_are_skipped(self):
        self.managed_edit()
        for name in ('deleted.py', 'linked.py'):
            (self.repo / name).write_text('tracked\n')
        self.git(self.repo, 'add', 'deleted.py', 'linked.py')
        self.git(self.repo, 'commit', '-qm', 'add targets')
        (self.repo / 'deleted.py').unlink()
        (self.repo / 'linked.py').unlink()
        (self.repo / 'linked.py').symlink_to(self.repo / 'gateway.py')
        module = self.prepare_managed()
        with mock.patch.dict('os.environ', self.env):
            reference = module['prepare'](self.repo)
        self.assertTrue(reference.startswith('refs/hermes-managed/pre-upgrade/'))

    def test_undecodable_files_are_skipped(self):
        self.managed_edit()
        binary = self.repo / 'binary.py'
        binary.write_bytes(b'\xff\xfe\n')
        self.git(self.repo, 'add', 'binary.py')
        self.git(self.repo, 'commit', '-qm', 'add binary')
        binary.write_bytes(b'\xff\xff\n')
        module = self.prepare_managed()
        with mock.patch.dict('os.environ', self.env):
            reference = module['prepare'](self.repo)
        self.assertTrue(reference.startswith('refs/hermes-managed/pre-upgrade/'))
        self.assertEqual(binary.read_bytes(), b'\xff\xff\n')

    def test_empty_recovery_snapshot_aborts_before_writing(self):
        _, current = self.managed_edit()
        module = self.prepare_managed()
        real_git = module['git']

        def git(root, *args):
            if args[:2] == ('stash', 'create'):
                return b''
            return real_git(root, *args)

        with mock.patch.dict(module['prepare'].__globals__, {'git': git}), \
                mock.patch.dict('os.environ', self.env), \
                self.assertRaisesRegex(RuntimeError, 'Cannot preserve the pre-upgrade working tree'):
            module['prepare'](self.repo)
        self.assertEqual((self.repo / 'gateway.py').read_text(), current)

    def test_cli_reports_recovery_then_reports_nothing_to_retire(self):
        self.managed_edit()
        script = str(Path(__file__).with_name('prepare-patch-upgrade.py'))
        outputs = []
        for _ in range(2):
            output = io.StringIO()
            with mock.patch.dict('os.environ', self.env), contextlib.redirect_stdout(output), \
                    mock.patch.object(sys, 'argv', ['prepare-patch-upgrade.py', str(self.repo)]):
                runpy.run_path(script, run_name='__main__')
            outputs.append(output.getvalue().strip())
        self.assertRegex(outputs[0], r'^Managed patches retired; recovery: refs/hermes-managed/pre-upgrade/\d+$')
        self.assertEqual(outputs[1], 'No recorded patches to retire')


if __name__ == '__main__':
    unittest.main()

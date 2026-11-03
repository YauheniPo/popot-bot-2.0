"""Retire only exact recorded managed edits; preserve operator edits and recovery."""
import hashlib
import json
from pathlib import Path
import runpy
import unittest
from unittest import mock

from hermes.runtime import test_installer_pin


class PatchUpgradeTests(unittest.TestCase):
    source = test_installer_pin.FIXTURE
    setUp = test_installer_pin.InstallerPinTests.setUp
    git = test_installer_pin.InstallerPinTests.git

    def prepare_managed(self):
        return runpy.run_path(str(Path(__file__).with_name('prepare-patch-upgrade.py')))

    def managed_edit(self, *, operator=False):
        original = (self.repo / 'gateway.py').read_text()
        block = '# Local Hermes: fixture\nmanaged value\n'
        current = original.replace('default\n', '\n' + block)
        if operator:
            current = current.replace('old upstream', 'operator setting')
        (self.repo / 'gateway.py').write_text(current)
        (self.repo / '.local-hermes-patches.json').write_text(json.dumps({
            '# Local Hermes: fixture': {'source': block, 'digest': hashlib.sha256(block.encode()).hexdigest()}}))
        return original, current

    def test_retires_recorded_patch_preserves_operator_edit_and_recovery(self):
        original, current = self.managed_edit(operator=True)
        module = self.prepare_managed()
        with mock.patch.dict('os.environ', self.env):
            reference = module['prepare'](self.repo)
        self.assertEqual((self.repo / 'gateway.py').read_text(), original.replace('old upstream', 'operator setting'))
        self.assertEqual(self.git(self.repo, 'show', f'{reference}:gateway.py'), current)
        self.assertEqual(self.git(self.repo, 'diff', '--cached'), '')
        self.assertIsNone(module['prepare'](self.repo))

    def test_modified_managed_block_is_preserved(self):
        _, current = self.managed_edit()
        current = current.replace('managed value', 'operator inside patch')
        (self.repo / 'gateway.py').write_text(current)
        self.assertIsNone(self.prepare_managed()['prepare'](self.repo))
        self.assertEqual((self.repo / 'gateway.py').read_text(), current)

    def test_overlapping_recorded_blocks_are_retired_as_one_change(self):
        retire = self.prepare_managed()['retire']
        first = '# Local Hermes: first\nfirst = True\n'
        second = '# Local Hermes: second\nsecond = True\n'
        original = 'before\nafter\n'
        current = 'before\n' + first + second + 'after\n'
        records = {marker: {'source': block, 'digest': hashlib.sha256(block.encode()).hexdigest()}
                   for marker, block in (('# Local Hermes: first', first + second[:10]),
                                         ('# Local Hermes: second', second))}
        self.assertEqual(retire(original, current, records), original)

    def test_failure_to_save_recovery_leaves_files_untouched(self):
        _, current = self.managed_edit()
        module = self.prepare_managed()
        real_git = module['git']
        def git(root, *args):
            if args[0] == 'update-ref':
                raise RuntimeError('cannot save recovery')
            return real_git(root, *args)
        with mock.patch.dict(module['prepare'].__globals__, {'git': git}), mock.patch.dict('os.environ', self.env):
            with self.assertRaisesRegex(RuntimeError, 'cannot save recovery'):
                module['prepare'](self.repo)
        self.assertEqual((self.repo / 'gateway.py').read_text(), current)

    def test_staged_edits_are_rejected_before_mutation(self):
        _, current = self.managed_edit()
        self.git(self.repo, 'add', 'gateway.py')
        with self.assertRaisesRegex(RuntimeError, 'staged'):
            self.prepare_managed()['prepare'](self.repo)
        self.assertEqual((self.repo / 'gateway.py').read_text(), current)

"""Managed Swarm provisioning must preserve work and be repeatable."""
import importlib.util
import io
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch
import yaml

PATH = Path(__file__).with_name('workspace-swarm.py')


class SwarmTests(unittest.TestCase):
    def test_role_block_migrates_legacy_without_losing_user_edits(self):
        spec = importlib.util.spec_from_file_location('swarm_roles', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        legacy = '# Managed Hermes Swarm\nLegacy generated text\n'
        migrated = module.role_soul(legacy, legacy)
        self.assertEqual(migrated.count('Legacy generated text'), 1)
        edited = legacy + '\nUser edit must survive\n'
        self.assertIn(edited, module.role_soul(edited, legacy))
        for malformed in (module.SOUL_BEGIN, module.SOUL_END,
                          module.SOUL_END + module.SOUL_BEGIN):
            with self.assertRaisesRegex(ValueError, 'Malformed'):
                module.role_soul(malformed, legacy)

    def test_cli_uses_effective_ansible_policy_from_stdin(self):
        spec = importlib.util.spec_from_file_location('swarm_setup', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        policy = {'workers': {'custom': {'role': 'Custom', 'instructions': 'Inspect.'}}}
        settings = yaml.safe_dump({'vps_workspace_ui': {'swarm': policy}})
        argv = ['swarm', '--home', '/home/test/.hermes', '--user-home', '/home/test',
                '--repo', '/repo', '--hermes-bin', '/bin/hermes', '--settings', '-']
        with patch.object(module.sys, 'argv', argv), patch.object(module.sys, 'stdin', io.StringIO(settings)), \
                patch.object(module, 'provision', return_value=0) as provision, \
                patch.object(module.sys, 'stdout', io.StringIO()):
            module.main()
        self.assertEqual(provision.call_args.args[-1], policy)

    def test_provision_preserves_worktrees_and_private_profile_state(self):
        spec = importlib.util.spec_from_file_location('swarm_setup', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            user = Path(temp)
            home = user / '.hermes'
            home.mkdir()
            (home / 'config.yaml').write_text('model: {provider: test, default: model-a}\n')
            (home / '.env').write_text('FIXTURE_ONLY=yes\n')
            repo = user / 'repo'
            repo.mkdir()
            calls = []

            def run(args, **kwargs):
                calls.append(args)
                if 'add' in args:
                    Path(args[-2]).mkdir(parents=True)
                return subprocess.CompletedProcess(args, 0, str(repo / '.git') + '\n', '')

            policy = {'workers': {'builder': {'role': 'Builder', 'description': 'Build and test scoped changes.',
                                               'instructions': 'Scoped edits.'}}}
            module.provision(home, user, repo, user / 'bin/hermes', policy, run)
            profile = home / 'profiles/builder'
            soul_policy = (profile / 'SOUL.md').read_text()
            for rule in ('Do not assume shared memory', 'memory candidates', "user's language", 'verified evidence'):
                self.assertIn(rule, soul_policy)
            config = yaml.safe_load((profile / 'config.yaml').read_text())
            self.assertEqual(config['description'], policy['workers']['builder']['description'])
            self.assertNotIn('system_prompt', config)
            self.assertEqual(config['approvals']['mode'], 'manual')
            self.assertEqual(config['terminal']['cwd'], str(home / 'swarm/worktrees/builder'))
            self.assertTrue((profile / '.env').is_symlink())
            wrapper = (user / '.local/bin/builder').read_text()
            self.assertNotIn('--yolo', wrapper)
            self.assertIn('"$@"', wrapper)
            (profile / 'keep.txt').write_text('user-data')
            (home / 'swarm/worktrees/builder/dirty.txt').write_text('unfinished work')
            self.assertEqual(module.provision(home, user, repo, user / 'bin/hermes', policy, run), 0)
            soul = profile / 'SOUL.md'
            soul.write_text(soul.read_text() + '\nMy personal preference: concise reports.\n')
            config['description'] = 'My custom description'
            (profile / 'config.yaml').write_text(yaml.safe_dump(config))
            policy['workers']['builder']['instructions'] = 'Updated managed instructions.'
            module.provision(home, user, repo, user / 'bin/hermes', policy, run)
            self.assertIn('My personal preference', soul.read_text())
            self.assertIn('Updated managed instructions.', soul.read_text())
            self.assertEqual(yaml.safe_load((profile / 'config.yaml').read_text())['description'], 'My custom description')
            self.assertEqual(module.provision(home, user, repo, user / 'bin/hermes', policy, run), 0)
            self.assertEqual(sum('add' in cmd for cmd in calls), 1)
            self.assertEqual((profile / 'keep.txt').read_text(), 'user-data')
            self.assertEqual((home / 'swarm/worktrees/builder/dirty.txt').read_text(), 'unfinished work')
            self.assertEqual((profile / 'config.yaml').stat().st_mode & 0o777, 0o600)

    def test_rejects_unmanaged_profile_before_creating_worktree(self):
        spec = importlib.util.spec_from_file_location('swarm_setup', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'profiles/builder').mkdir(parents=True)
            (root / 'config.yaml').write_text('model: {provider: test, default: fixture}\n')
            with self.assertRaisesRegex(ValueError, 'unmanaged'):
                module.provision(root, root, root, root/'hermes',
                                 {'workers': {'builder': {'role': 'Builder', 'instructions': 'Test'}}})

    @staticmethod
    def _module():
        spec = importlib.util.spec_from_file_location('swarm_setup', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _swarm_env(self, temp, model='model: {provider: test, default: fixture}\n'):
        user = Path(temp)
        home = user / '.hermes'
        home.mkdir()
        (home / 'config.yaml').write_text(model)
        repo = user / 'repo'
        repo.mkdir()
        return home, user, repo

    def _run_stub(self, repo, fail_on=()):
        def run(args, **kwargs):
            for token in fail_on:
                if token in args:
                    raise subprocess.CalledProcessError(1, args)
            if 'add' in args:
                Path(args[-2]).mkdir(parents=True)
            return subprocess.CompletedProcess(args, 0, str(repo / '.git') + '\n', '')
        return run

    def test_provision_rejects_invalid_policy_and_unconfigured_model(self):
        spec = importlib.util.spec_from_file_location('swarm_setup', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            home, user, repo = self._swarm_env(temp)
            for policy in ({'workers': {}}, {'workers': []},
                           {'workers': {'Builder': {'role': 'B', 'instructions': 'x'}}}):
                with self.subTest(policy=policy), self.assertRaises(ValueError):
                    module.provision(home, user, repo, user / 'bin/hermes', policy, self._run_stub(repo))
            for bad_model in ('{}\n', 'model: {provider: test}\n',
                              'model: {provider: "", default: fixture}\n'):
                (home / 'config.yaml').write_text(bad_model)
                with self.subTest(model=bad_model), self.assertRaisesRegex(ValueError, 'shared Hermes model'):
                    module.provision(home, user, repo, user / 'bin/hermes',
                                     {'workers': {'builder': {'role': 'B', 'instructions': 'x'}}},
                                     self._run_stub(repo))

    def test_provision_rejects_symlinked_managed_directories(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as temp:
            home, user, repo = self._swarm_env(temp)
            outside = user / 'outside'
            outside.mkdir()
            (home / 'swarm').symlink_to(outside)
            policy = {'workers': {'builder': {'role': 'B', 'instructions': 'x'}}}
            with self.assertRaisesRegex(ValueError, 'must not be symlinks'):
                module.provision(home, user, repo, user / 'bin/hermes', policy, self._run_stub(repo))

    def test_save_text_rejects_symlinks_and_reports_repeated_content(self):
        spec = importlib.util.spec_from_file_location('swarm_setup', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'file.txt'
            target.symlink_to(root / 'elsewhere.txt')
            with self.assertRaisesRegex(ValueError, 'must not be a symlink'):
                module.save_text(target, 'content')
            real = root / 'real.txt'
            self.assertEqual(module.save_text(real, 'first'), 1)
            self.assertEqual(module.save_text(real, 'first'), 0)
            self.assertEqual(real.stat().st_mode & 0o777, 0o600)

    def test_role_soul_rejects_out_of_order_markers(self):
        spec = importlib.util.spec_from_file_location('swarm_setup', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with self.assertRaisesRegex(ValueError, 'Malformed'):
            module.role_soul(module.SOUL_END + module.SOUL_BEGIN, 'policy')

    def test_provision_rejects_a_worktree_from_another_repository(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as temp:
            home, user, repo = self._swarm_env(temp)
            worktree = home / 'swarm/worktrees/builder'
            worktree.mkdir(parents=True)

            def run(args, **kwargs):
                # The existing worktree reports a different common git dir.
                common = user / 'other.git' if str(worktree) in args else repo / '.git'
                return subprocess.CompletedProcess(args, 0, str(common) + '\n', '')

            policy = {'workers': {'builder': {'role': 'B', 'instructions': 'x'}}}
            with self.assertRaisesRegex(ValueError, 'different repository'):
                module.provision(home, user, repo, user / 'bin/hermes', policy, run)

    def test_cli_reports_a_provisioning_failure_without_leaking_details(self):
        module = self._module()
        settings = yaml.safe_dump({'vps_workspace_ui': {'swarm': {'workers': {}}}})
        argv = ['swarm', '--home', '/home/test/.hermes', '--user-home', '/home/test',
                '--repo', '/repo', '--hermes-bin', '/bin/hermes', '--settings', '-']
        stderr = io.StringIO()
        with patch.object(module.sys, 'argv', argv), \
             patch.object(module.sys, 'stdin', io.StringIO(settings)), \
             patch.object(module.sys, 'stderr', stderr), \
             self.assertRaises(SystemExit) as exit_code:
            module.main()
        self.assertEqual(exit_code.exception.code, 1)
        self.assertIn('Swarm provisioning failed (ValueError)', stderr.getvalue())

    def test_provision_rejects_a_symlinked_worktree_and_soul(self):
        module = self._module()
        policy = {'workers': {'builder': {'role': 'B', 'instructions': 'x'}}}
        with tempfile.TemporaryDirectory() as temp:
            home, user, repo = self._swarm_env(temp)
            outside = user / 'outside'
            outside.mkdir()
            (home / 'swarm/worktrees').mkdir(parents=True)
            (home / 'swarm/worktrees/builder').symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'worktree must not be a symlink'):
                module.provision(home, user, repo, user / 'bin/hermes', policy, self._run_stub(repo))

    def test_provision_rejects_a_symlinked_soul_and_an_unmanaged_wrapper(self):
        module = self._module()
        policy = {'workers': {'builder': {'role': 'B', 'instructions': 'x'}}}
        with tempfile.TemporaryDirectory() as temp:
            home, user, repo = self._swarm_env(temp)
            profile = home / 'profiles/builder'
            profile.mkdir(parents=True)
            (profile / '.managed-swarm').write_text('managed')
            outside = user / 'outside.md'
            outside.write_text('external')
            (profile / 'SOUL.md').symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'SOUL must not be a symlink'):
                module.provision(home, user, repo, user / 'bin/hermes', policy, self._run_stub(repo))

    def test_provision_rejects_an_unmanaged_existing_wrapper(self):
        module = self._module()
        policy = {'workers': {'builder': {'role': 'B', 'instructions': 'x'}}}
        with tempfile.TemporaryDirectory() as temp:
            home, user, repo = self._swarm_env(temp)
            wrapper = user / '.local/bin/builder'
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text('#!/bin/sh\n# operator-owned wrapper\necho hi\n')
            with self.assertRaisesRegex(ValueError, 'unmanaged Swarm wrapper'):
                module.provision(home, user, repo, user / 'bin/hermes', policy, self._run_stub(repo))

    def test_cli_reads_settings_from_a_file(self):
        module = self._module()
        policy = {'workers': {'custom': {'role': 'Custom', 'instructions': 'Inspect.'}}}
        with tempfile.TemporaryDirectory() as temp:
            settings_path = Path(temp) / 'settings.yml'
            settings_path.write_text(yaml.safe_dump({'vps_workspace_ui': {'swarm': policy}}))
            argv = ['swarm', '--home', '/home/test/.hermes', '--user-home', '/home/test',
                    '--repo', '/repo', '--hermes-bin', '/bin/hermes', '--settings', str(settings_path)]
            with patch.object(module.sys, 'argv', argv), \
                 patch.object(module, 'provision', return_value=3) as provision, \
                 patch.object(module.sys, 'stdout', io.StringIO()) as stdout:
                module.main()
        self.assertEqual(provision.call_args.args[-1], policy)
        self.assertIn('3 change(s)', stdout.getvalue())

    def test_module_entrypoint_provisions_from_stdin(self):
        # The module-level guard is only reachable via runpy; feed a policy with
        # no workers so provisioning fails fast and nothing is written.
        settings = yaml.safe_dump({'vps_workspace_ui': {'swarm': {'workers': {}}}})
        argv = ['workspace-swarm.py', '--home', '/home/test/.hermes', '--user-home', '/home/test',
                '--repo', '/repo', '--hermes-bin', '/bin/hermes', '--settings', '-']
        with mock.patch.object(sys, 'argv', argv), \
             mock.patch.object(sys, 'stdin', io.StringIO(settings)), \
             mock.patch.object(sys, 'stderr', io.StringIO()) as stderr, \
             self.assertRaises(SystemExit) as exit_code:
            runpy.run_path(str(PATH), run_name='__main__')
        self.assertEqual(exit_code.exception.code, 1)
        self.assertIn('Swarm provisioning failed', stderr.getvalue())

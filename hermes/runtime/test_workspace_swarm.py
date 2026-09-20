"""Managed Swarm provisioning must preserve work and be repeatable."""
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
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

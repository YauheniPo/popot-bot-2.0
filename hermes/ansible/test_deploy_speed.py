"""Behavior checks for the full deploy's smaller number of remote calls."""

import getpass
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ANSIBLE = Path(__file__).parent


def all_tasks(tasks):
    for task in tasks:
        yield task
        for key in ('block', 'rescue', 'always'):
            yield from all_tasks(task.get(key, []))


class DeploySpeedTests(unittest.TestCase):
    def setUp(self):
        self.tasks = list(all_tasks(yaml.safe_load((ANSIBLE / 'playbook.yml').read_text())[0]['tasks']))

    def test_backup_ownership_uses_native_scan(self):
        task = next(t for t in self.tasks if t.get('name') == 'Normalize Hermes home ownership before backup')
        self.assertIn('ansible.builtin.command', task)
        self.assertIn('{{ hermes_bundle_dir }}/runtime/normalize-ownership.py',
                      task['ansible.builtin.command']['argv'])
        self.assertNotIn('failed_when', task)
        self.assertIn('ownership-changed=true', task['changed_when'])

    @unittest.skipUnless(shutil.which('rsync') and shutil.which('ansible-playbook'), 'rsync and Ansible are required')
    def test_real_synchronize_action_transfers_required_helpers(self):
        sync = deepcopy(next(t for t in self.tasks if 'ansible.posix.synchronize' in t))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / 'source', root / 'bundle'
            play_directory = source / 'ansible'
            play_directory.mkdir(parents=True)
            target.mkdir()
            allowed = ['runtime/normalize-ownership.py', 'runtime/deployment-notify.py',
                       'runtime/verify-update-state.py', 'runtime/apply-hermes-patches.py',
                       'deploy/run.sh', 'ops/health.sh', 'observability/rules/a.yml',
                       'config/defaults.yml', 'skills/demo/SKILL.md', 'vscode-server/Dockerfile']
            excluded = ['ansible/group_vars/all/vault.yml', '.env', 'docs/private.md', 'skills/demo/.env']
            for name in allowed + excluded:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture: true\n' if name.endswith('vault.yml') else 'fixture')
            (target / 'operator-file').write_text('keep')
            sync['register'] = 'bundle_sync'
            play = [{'hosts': 'localhost', 'gather_facts': False, 'vars': {
                'ansible_python_interpreter': sys.executable, 'hermes_bundle_dir': str(target),
            }, 'tasks': [sync]}]
            path = play_directory / 'check.yml'
            path.write_text(yaml.safe_dump(play))
            for attempt in range(2):
                result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(path)],
                                        capture_output=True, text=True, timeout=30,
                                        env={**os.environ, 'ANSIBLE_CONFIG': str(ANSIBLE / 'ansible.cfg')})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for name in allowed:
                    self.assertTrue((target / name).is_file(), f'Missing bundle file: {name}')
                for name in excluded:
                    self.assertFalse((target / name).exists(), name)
                self.assertEqual((target / 'operator-file').read_text(), 'keep')
                if attempt:
                    self.assertIn('changed=0', result.stdout)

    def test_missing_bundle_helpers_fail_before_gateway_maintenance(self):
        guard = next((t for t in self.tasks if t.get('name') == 'Verify required runtime helpers before maintenance'), None)
        self.assertIsNotNone(guard, 'Verify the synchronized bundle before any service is stopped')
        marker = next(t for t in self.tasks if t.get('name') == 'Mark maintenance before stopping an existing gateway')
        self.assertLess(self.tasks.index(guard), self.tasks.index(marker))
        self.assertNotIn('failed_when', guard)
        self.assertFalse(guard.get('changed_when', True))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = guard['ansible.builtin.command']['argv']
            command = [part.replace('{{ hermes_bundle_dir }}', str(root)) for part in command]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('normalize-ownership.py', result.stderr)
            self.assertIn('deployment-notify.py', result.stderr)
            for name in ('normalize-ownership.py', 'deployment-notify.py', 'verify-update-state.py', 'apply-hermes-patches.py'):
                path = root / 'runtime' / name
                path.parent.mkdir(exist_ok=True)
                path.write_text('fixture')
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('rsync'), 'rsync is required')
    def test_single_bundle_sync_preserves_allowlist_secrets_and_checksum_updates(self):
        syncs = [t['ansible.posix.synchronize'] for t in self.tasks if 'ansible.posix.synchronize' in t]
        self.assertEqual(len(syncs), 1, 'The bundle must use one rsync connection')
        options = syncs[0]
        self.assertTrue(options['checksum'])
        self.assertFalse(options['owner'])
        self.assertFalse(options['group'])
        self.assertFalse(options.get('delete', False))
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            source.mkdir()
            target.mkdir()
            allowed = ['deploy/run.sh', 'ops/health.sh', 'observability/rules/a.yml',
                       'config/defaults.yml', 'runtime/helper.py', 'skills/demo/SKILL.md',
                       'vscode-server/Dockerfile']
            excluded = ['ansible/group_vars/all/vault.yml', '.env', 'personal.txt',
                        *['skills/' + prefix + name for prefix in ('', 'demo/', 'demo/nested/')
                          for name in ('.env', '.env.local', '.envrc', 'dotenv', 'env', 'env.dev',
                                       'a.secret', 'a.key', 'a.pem', 'credentials.yml',
                                       'credentials.yaml', 'vault.yml', 'vault.yaml', 'id_rsa',
                                       'id_ed25519', 'a.p12', 'a.pfx', '.netrc', 'test_demo.py',
                                       'module.pyc', '__pycache__/data', 'node_modules/data')]]
            for name in allowed + excluded:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('old')
            (target / 'operator-file').write_text('keep')
            command = ['rsync', '-a', '--no-owner', '--no-group', '--checksum',
                       *options['rsync_opts'], str(source) + '/', str(target) + '/']
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            for name in allowed:
                self.assertEqual((target / name).read_text(), 'old', name)
            for name in excluded:
                self.assertFalse((target / name).exists(), name)
            self.assertEqual((target / 'operator-file').read_text(), 'keep')
            helper = target / 'runtime/helper.py'
            metadata = helper.stat()
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(helper.stat().st_mtime_ns, metadata.st_mtime_ns)
            changed = source / 'runtime/helper.py'
            previous = changed.stat()
            changed.write_text('new')
            os.utime(changed, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(helper.read_text(), 'new')

    def test_tts_uses_pinned_path_before_scoped_fallback(self):
        tasks = yaml.safe_load((ANSIBLE / 'tasks/services.yml').read_text())
        primary = next(t for t in tasks if t.get('name') == 'Locate the installed Hermes Edge TTS implementation')
        self.assertIn('ansible.builtin.stat', primary)
        self.assertEqual(primary['ansible.builtin.stat']['path'],
                         '{{ hermes_home }}/hermes-agent/tools/tts_tool.py')
        fallback = next(t for t in tasks if 'ansible.builtin.find' in t)
        self.assertIn('not hermes_tts_tool.stat.exists', fallback['when'])
        self.assertNotIn('{{ hermes_home }}', fallback['ansible.builtin.find']['paths'])
        self.assertNotIn('{{ hermes_user_home }}/.local', fallback['ansible.builtin.find']['paths'])

    @unittest.skipUnless(shutil.which('rsync') and shutil.which('ansible-playbook'), 'rsync and Ansible are required')
    def test_private_controller_directory_does_not_remove_shared_bundle_access(self):
        sync_index = next(i for i, t in enumerate(self.tasks) if 'ansible.posix.synchronize' in t)
        repairs = [t for t in self.tasks[sync_index + 1:]
                   if t.get('ansible.builtin.file', {}).get('path') == '{{ hermes_bundle_dir }}'
                   and t['ansible.builtin.file'].get('mode') == '0755']
        self.assertEqual(len(repairs), 1, 'Restore root bundle access after rsync copies source directory mode')
        self.assertFalse(repairs[0]['ansible.builtin.file'].get('recurse', False))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / 'source', root / 'bundle'
            (source / 'runtime').mkdir(parents=True)
            data = source / 'runtime/data'
            data.write_text('fixture')
            data.chmod(0o600)
            source.chmod(0o700)
            target.mkdir(mode=0o755)
            options = self.tasks[sync_index]['ansible.posix.synchronize']['rsync_opts']
            subprocess.run(['rsync', '-a', '--no-owner', '--no-group', *options,
                            str(source) + '/', str(target) + '/'], check=True, capture_output=True)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            play = [{'hosts': 'localhost', 'gather_facts': False,
                     'vars': {'ansible_python_interpreter': sys.executable, 'hermes_bundle_dir': str(target)},
                     'tasks': repairs}]
            path = root / 'check.yml'
            path.write_text(yaml.safe_dump(play))
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(path)],
                                    capture_output=True, text=True, timeout=30,
                                    env={**os.environ, 'ANSIBLE_CONFIG': str(ANSIBLE / 'ansible.cfg')})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertEqual((target / 'runtime/data').stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible is required')
    def test_tts_fast_path_fallback_and_absence_execute_real_tasks(self):
        tasks = yaml.safe_load((ANSIBLE / 'tasks/services.yml').read_text())
        primary = next(t for t in tasks if t.get('name') == 'Locate the installed Hermes Edge TTS implementation')
        self.assertIn('ansible.builtin.stat', primary)
        fallback = next(t for t in tasks if 'ansible.builtin.find' in t)
        apply = next(t for t in tasks if t.get('name') == 'Install transient Edge TTS retry when the upstream tool is found')
        absent = next(t for t in tasks if t.get('name') == 'Report when the upstream Edge TTS implementation cannot be located')
        apply['ansible.builtin.command']['argv'][:2] = [sys.executable, '-c', 'import sys; print(sys.argv[1])']
        for kind in ('pinned', 'legacy', 'absent'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / '.hermes'
                paths = {'pinned': home / 'hermes-agent/tools/tts_tool.py',
                         'legacy': root / '.local/lib/legacy/tools/tts_tool.py'}
                for parent in (home / 'tools', root / '.local/lib', home / 'hermes-agent/tools'):
                    parent.mkdir(parents=True, exist_ok=True)
                if kind != 'absent':
                    paths[kind].parent.mkdir(parents=True, exist_ok=True)
                    paths[kind].write_text('fixture')
                decoy = home / 'cache/tts_tool.py'
                decoy.parent.mkdir()
                decoy.write_text('not installed')
                check = {'ansible.builtin.assert': {'that': [
                    'hermes_tts_retry.skipped | default(false)' if kind == 'absent'
                    else 'hermes_tts_retry.stdout == ' + json.dumps(str(paths[kind])),
                    'hermes_tts_tool_files.skipped | default(false)' if kind == 'pinned'
                    else 'not (hermes_tts_tool_files.skipped | default(false))',
                ]}}
                play = [{'hosts': 'localhost', 'gather_facts': False, 'vars': {
                    'ansible_python_interpreter': sys.executable, 'hermes_home': str(home),
                    'hermes_user_home': str(root), 'hermes_user': getpass.getuser(),
                }, 'tasks': [primary, fallback, apply, absent, check]}]
                path = root / 'check.yml'
                path.write_text(yaml.safe_dump(play))
                result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(path)],
                                        capture_output=True, text=True, timeout=30,
                                        env={**os.environ, 'ANSIBLE_CONFIG': str(ANSIBLE / 'ansible.cfg')})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_notifications_are_one_waited_batch_with_secret_stdin(self):
        tasks = yaml.safe_load((ANSIBLE / 'tasks/deployment-notify.yml').read_text())
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertIn('ansible.builtin.command', task)
        self.assertNotIn('loop', task)
        self.assertNotIn('async', task)
        self.assertTrue(task['no_log'])
        self.assertIs(task['failed_when'], False)
        command = task['ansible.builtin.command']
        self.assertIn('deployment-notify.py', ' '.join(command['argv']))
        self.assertNotIn('hermes_secret_env', ' '.join(command['argv']))
        self.assertIn('TELEGRAM_BOT_TOKEN', command['stdin'])
        self.assertIn('TELEGRAM_ALLOWED_USERS', command['stdin'])
        self.assertIn('to_json', command['stdin'])

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible is required')
    def test_real_notification_task_keeps_secrets_private_and_failure_best_effort(self):
        task = yaml.safe_load((ANSIBLE / 'tasks/deployment-notify.yml').read_text())[0]
        task['register'] = 'notification_result'
        for status in (0, 1):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'runtime').mkdir()
                helper = root / 'runtime/deployment-notify.py'
                helper.write_text(
                    "import json, sys\np = json.load(sys.stdin)\n"
                    "assert p == {'token': 'test-only-token', 'chat_ids': ['1', '2'], "
                    "'text': 'test-only-message'}\n"
                    "assert sys.argv[1:] == ['--timeout', '7', '--workers', '2']\n"
                    "print(json.dumps({'sent': 2, 'failed': 0}))\nsys.exit(" + str(status) + ')\n')
                play = [{'hosts': 'localhost', 'gather_facts': False, 'vars': {
                    'ansible_python_interpreter': sys.executable,
                    'hermes_bundle_dir': str(root), 'hermes_manage_secret_env': True,
                    'hermes_secret_env': {'TELEGRAM_BOT_TOKEN': 'test-only-token',
                                          'TELEGRAM_ALLOWED_USERS': '1,2'},
                    'hermes_deployment_notification': 'test-only-message',
                    'vps_deploy': {'notifications': {'timeout_seconds': 7, 'max_parallel': 2}},
                }, 'tasks': [task, {'ansible.builtin.assert': {'that': [
                    'notification_result.rc == ' + str(status),
                    'not notification_result.failed', 'not notification_result.changed',
                ]}}]}]
                path = root / 'check.yml'
                path.write_text(yaml.safe_dump(play))
                result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(path)],
                                        capture_output=True, text=True, timeout=30,
                                        env={**os.environ, 'ANSIBLE_CONFIG': str(ANSIBLE / 'ansible.cfg')})
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                self.assertNotIn('test-only-token', output)
                self.assertNotIn('test-only-message', output)
                self.assertIn('[timing]', output)
                self.assertNotIn('Failure using method', output)

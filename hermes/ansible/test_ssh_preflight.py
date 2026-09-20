"""Bounded controller-side SSH authentication before remote deployment tasks."""

import importlib.util
from pathlib import Path
import sys
import time
import unittest

import yaml


PLUGIN = Path(__file__).parent / 'action_plugins/hermes_ssh_preflight.py'
SPEC = importlib.util.spec_from_file_location('hermes_ssh_preflight', PLUGIN)
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


class SshPreflightTests(unittest.TestCase):
    def test_authentication_is_checked_before_explicit_bounded_facts(self):
        play = yaml.safe_load((PLUGIN.parents[1] / 'playbook.yml').read_text())[0]
        self.assertFalse(play['gather_facts'])
        self.assertIn('hermes_ssh_preflight', play['pre_tasks'][0])
        self.assertIn('ansible.builtin.setup', play['pre_tasks'][1])
        self.assertGreater(play['pre_tasks'][1]['timeout'], 0)

    def test_only_tailnet_destinations_use_browser_check(self):
        for host in ('100.83.123.65', 'vps.example.ts.net.', 'fd7a:115c:a1e0::1'):
            self.assertTrue(preflight.is_tailnet_host(host))
        for host in ('203.0.113.2', '127.0.0.1', 'not-ts.net', '100.1.1.1'):
            self.assertFalse(preflight.is_tailnet_host(host))

    def test_probe_succeeds_without_authentication_prompt(self):
        result = preflight.probe([sys.executable, '-c', 'pass'], 2, lambda _: None, lambda _: None)
        self.assertEqual(result, 'ready')

    def test_closed_browser_cannot_leave_a_stuck_ssh_process(self):
        events = []
        command = [sys.executable, '-u', '-c',
                   "import sys,time; print('# To authenticate, visit: https://login.tailscale.com/a/test123', file=sys.stderr, flush=True); time.sleep(30)"]
        started = time.monotonic()
        self.assertEqual(preflight.probe(command, .2, events.append, lambda _: None), 'auth_timeout')
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(events, ['https://login.tailscale.com/a/test123'])

    def test_headless_authentication_fails_immediately(self):
        command = [sys.executable, '-u', '-c',
                   "import sys,time; print('# To authenticate, visit: https://login.tailscale.com/a/test123', file=sys.stderr, flush=True); time.sleep(30)"]
        started = time.monotonic()
        self.assertEqual(preflight.probe(command, 10, lambda _: False, lambda _: None), 'auth_required')
        self.assertLess(time.monotonic() - started, 3)

    def test_output_without_newlines_is_bounded_by_deadline(self):
        command = [sys.executable, '-u', '-c', "import sys;\nwhile True: sys.stderr.write('x'*65536)"]
        started = time.monotonic()
        self.assertEqual(preflight.probe(command, .2, lambda _: None, lambda _: None), 'timeout')
        self.assertLess(time.monotonic() - started, 3)

    def test_untrusted_url_does_not_open_browser(self):
        events = []
        command = [sys.executable, '-c',
                   "import sys; print('# To authenticate, visit: https://login.tailscale.com.evil/a/test123', file=sys.stderr); sys.exit(255)"]
        self.assertEqual(preflight.probe(command, 2, events.append, lambda _: None), 'failed')
        self.assertEqual(events, [])

    def test_retry_is_bounded_and_does_not_retry_host_key_failure(self):
        from unittest.mock import Mock
        run = Mock(side_effect=['auth_timeout', 'ready'])
        self.assertEqual(preflight.retry_probe(run, 2, lambda _: None), 'ready')
        self.assertEqual(run.call_count, 2)
        run = Mock(return_value='auth_timeout')
        self.assertEqual(preflight.retry_probe(run, 2, lambda _: None), 'auth_timeout')
        self.assertEqual(run.call_count, 2)
        run = Mock(return_value='host_key')
        self.assertEqual(preflight.retry_probe(run, 2, lambda _: None), 'host_key')
        self.assertEqual(run.call_count, 1)

    def test_cancellation_reaps_the_probe(self):
        from unittest.mock import patch
        import subprocess
        children = []
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        def cancel(_):
            raise KeyboardInterrupt()

        command = [sys.executable, '-u', '-c',
                   "import sys,time; print('https://login.tailscale.com/a/test123', file=sys.stderr, flush=True); time.sleep(30)"]
        with patch.object(preflight.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaises(KeyboardInterrupt):
                preflight.probe(command, 10, cancel, lambda _: None)
        self.assertIsNotNone(children[0].poll())

    def test_ssh_probe_preserves_identity_without_reusing_a_control_socket(self):
        from types import SimpleNamespace
        options = {'host': '100.64.0.1', 'remote_user': 'operator', 'port': 2222,
                   'private_key_file': '/tmp/test key', 'ssh_args': '-o ControlPersist=60s',
                   'ssh_common_args': '-o ProxyJump=bastion', 'ssh_extra_args': ''}
        command = preflight.ssh_command(SimpleNamespace(get_option=options.get), SimpleNamespace())
        self.assertIn('ProxyJump=bastion', command)
        self.assertIn('/tmp/test key', command)
        self.assertLess(command.index('ControlPersist=no'), command.index('ControlPersist=60s'))
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertEqual(command[-5:], ['-p', '2222', '--', '100.64.0.1', 'true'])

    def test_worker_sigterm_restores_handler_after_cleanup(self):
        import os
        import signal
        previous = signal.getsignal(signal.SIGTERM)

        def terminate(_):
            os.kill(os.getpid(), signal.SIGTERM)

        command = [sys.executable, '-u', '-c',
                   "import sys,time; print('https://login.tailscale.com/a/test123', file=sys.stderr, flush=True); time.sleep(30)"]
        with self.assertRaises(KeyboardInterrupt):
            preflight.probe(command, 10, terminate, lambda _: None)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_password_ssh_falls_back_to_bounded_native_ansible_auth(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        action = object.__new__(preflight.ActionModule)
        action._task = SimpleNamespace(args={})
        action._connection = SimpleNamespace(transport='ssh', get_option={
            'host': '100.64.0.1', 'password': 'synthetic-test-password',
        }.get)
        action._play_context = SimpleNamespace()
        action._display = Mock()
        with patch.object(preflight.ActionBase, 'run', return_value={}), \
             patch.object(preflight, 'ssh_command', return_value=['ssh']), \
             patch.object(preflight, 'retry_probe', return_value='denied'):
            result = action.run(task_vars={})
        self.assertTrue(result['skipped'])
        self.assertNotIn('synthetic-test-password', str(result))


if __name__ == '__main__':
    unittest.main()

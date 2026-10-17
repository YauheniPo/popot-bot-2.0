"""Behavioral checks for deploy-time service and firewall reconciliation."""
import importlib.util
import io
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def firewall_status(private_first=True, ipv6=True):
    rows = []
    for suffix in (('', ' (v6)') if ipv6 else ('',)):
        pair = [f'22/tcp{suffix} on tailscale0 ALLOW IN Anywhere{suffix} # Hermes private SSH',
                f'22/tcp{suffix} DENY IN Anywhere{suffix} # Hermes deny public SSH']
        rows.extend(pair if private_first else reversed(pair))
    return 'Status: active\n' + '\n'.join(f'[{i}] {row}' for i, row in enumerate(rows, 1))


class FirewallReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.module = load_helper('reconcile-firewall')
        self.verbose = 'Status: active\nDefault: deny (incoming), allow (outgoing), deny (routed)\n'
        self.added = ("ufw deny 22/tcp comment 'Hermes deny public SSH'\n"
                      "ufw allow in on tailscale0 to any port 22 proto tcp comment 'Hermes private SSH'\n"
                      'ufw allow 443/tcp\n')

    def reconcile(self, initial, verbose=None, ipv6=True):
        reads = {('status', 'numbered'): [initial, firewall_status(ipv6=ipv6)],
                 ('status', 'verbose'): [verbose or self.verbose, self.verbose],
                 ('show', 'added'): [self.added]}
        def run(args):
            values = reads.get(tuple(args))
            return values.pop(0) if values else ''
        with mock.patch.object(self.module, 'run_ufw', side_effect=run) as command:
            changed = self.module.reconcile('tailscale0', 22, 'deny', 'allow', ipv6)
        return changed, [call.args[0] for call in command.call_args_list]

    def test_correct_rules_and_policies_do_not_mutate_firewall(self):
        for ipv6 in (True, False):
            with self.subTest(ipv6=ipv6):
                changed, calls = self.reconcile(firewall_status(ipv6=ipv6), ipv6=ipv6)
                self.assertFalse(changed)
                self.assertTrue(all(args[0] == 'status' for args in calls), calls)

    def test_wrong_order_repairs_both_families_and_preserves_unrelated_rules(self):
        changed, calls = self.reconcile(firewall_status(False))
        self.assertTrue(changed)
        prepends = [args for args in calls if args[0] == 'prepend']
        self.assertEqual([args[1] for args in prepends], ['deny', 'allow'])
        self.assertIn('tailscale0', prepends[1])
        deletes = [args for args in calls if args[:2] == ['--force', 'delete']]
        self.assertEqual(len(deletes), 2)
        self.assertTrue(all('443/tcp' not in args for args in deletes))
        self.assertNotIn(['--force', 'enable'], calls)

    def test_inactive_firewall_is_configured_before_enabling(self):
        changed, calls = self.reconcile('Status: inactive\n', 'Status: inactive\n')
        self.assertTrue(changed)
        enable = calls.index(['--force', 'enable'])
        self.assertLess(calls.index(['default', 'deny', 'incoming']), enable)
        self.assertLess(calls.index(['default', 'allow', 'outgoing']), enable)
        self.assertLess(next(i for i, args in enumerate(calls) if args[:2] == ['prepend', 'allow']), enable)

    def test_policy_drift_changes_only_affected_policy(self):
        changed, calls = self.reconcile(firewall_status(), self.verbose.replace('allow (outgoing)', 'deny (outgoing)'))
        self.assertTrue(changed)
        self.assertIn(['default', 'allow', 'outgoing'], calls)
        self.assertNotIn(['default', 'deny', 'incoming'], calls)
        self.assertFalse(any(args[0] == 'prepend' for args in calls))

    def test_missing_ipv6_or_early_public_allow_is_not_considered_current(self):
        for text in (firewall_status(ipv6=False), '[0] 22/tcp ALLOW IN Anywhere\n' + firewall_status()):
            with self.subTest(text=text):
                self.assertFalse(self.module.private_rules_current(text, 'tailscale0', 22, True))

    def test_failed_command_or_unrepaired_rules_cannot_report_success(self):
        with mock.patch.object(self.module, 'run_ufw', side_effect=subprocess.CalledProcessError(1, ['ufw'])):
            with self.assertRaises(subprocess.CalledProcessError):
                self.module.reconcile('tailscale0', 22, 'deny', 'allow', True)
        with mock.patch.object(self.module, 'run_ufw', return_value='Status: inactive\n'):
            with self.assertRaisesRegex(RuntimeError, 'verification'):
                self.module.reconcile('tailscale0', 22, 'deny', 'allow', True)

    def test_run_ufw_invokes_ufw_with_a_pinned_locale_and_returns_stdout(self):
        completed = subprocess.CompletedProcess(['ufw'], 0, stdout='Status: active\n', stderr='')
        with mock.patch('subprocess.run', return_value=completed) as run:
            output = self.module.run_ufw(['status', 'verbose'])
        self.assertEqual(output, 'Status: active\n')
        command, = run.call_args.args
        self.assertEqual(command, ['ufw', 'status', 'verbose'])
        kwargs = run.call_args.kwargs
        self.assertEqual((kwargs['check'], kwargs['capture_output'], kwargs['text']), (True, True, True))
        self.assertEqual(kwargs['timeout'], 30)
        self.assertEqual(kwargs['env']['LC_ALL'], 'C')

    def test_active_status_without_default_policies_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, 'default policies'):
            self.module.current_policies('Status: active\n')

    def test_invalid_interface_or_port_is_rejected_before_any_command(self):
        with mock.patch.object(self.module, 'run_ufw') as run:
            for interface, port in (('bad interface', 22), ('a' * 16, 22), ('tailscale0', 0), ('tailscale0', 65536)):
                with self.subTest(interface=interface, port=port):
                    with self.assertRaisesRegex(ValueError, 'Invalid private SSH'):
                        self.module.reconcile(interface, port, 'deny', 'allow', True)
            run.assert_not_called()

    def test_unsupported_policies_are_rejected_before_any_command(self):
        with mock.patch.object(self.module, 'run_ufw') as run:
            for incoming, outgoing in (('allow', 'allow'), ('deny', 'accept')):
                with self.subTest(incoming=incoming, outgoing=outgoing):
                    with self.assertRaisesRegex(ValueError, 'Unsupported private SSH'):
                        self.module.reconcile('tailscale0', 22, incoming, outgoing, True)
            run.assert_not_called()

    def test_main_reads_ipv6_policy_and_reports_the_result(self):
        argv = ['reconcile-firewall', '--interface', 'tailscale0', '--port', '22',
                '--incoming', 'deny', '--outgoing', 'allow']
        for text, ipv6 in (('IPV6=yes\n', True), ('IPV6=no\n', False)):
            with self.subTest(text=text), \
                    mock.patch.object(sys, 'argv', argv), \
                    mock.patch.object(Path, 'read_text', return_value=text), \
                    mock.patch.object(self.module, 'reconcile', return_value=True) as reconcile, \
                    mock.patch('sys.stdout', new_callable=io.StringIO) as out:
                self.module.main()
            reconcile.assert_called_once_with('tailscale0', 22, 'deny', 'allow', ipv6)
            self.assertEqual(out.getvalue().strip(), 'firewall-changed=true')

    def test_main_exits_nonzero_when_it_cannot_reconcile(self):
        argv = ['reconcile-firewall', '--interface', 'tailscale0', '--port', '22',
                '--incoming', 'deny', '--outgoing', 'allow']
        failures = (mock.patch.object(Path, 'read_text', side_effect=OSError('unreadable')),
                    mock.patch.object(Path, 'read_text', return_value='IPV6=maybe\n'),
                    mock.patch.object(Path, 'read_text', return_value='IPV6=yes\n'))
        for index, read_patch in enumerate(failures):
            with self.subTest(case=index), mock.patch.object(sys, 'argv', argv), read_patch, \
                    mock.patch.object(self.module, 'reconcile', side_effect=RuntimeError('boom')), \
                    mock.patch('sys.stderr', new_callable=io.StringIO) as err:
                with self.assertRaises(SystemExit) as exit_error:
                    self.module.main()
            self.assertEqual(exit_error.exception.code, 1)
            self.assertIn('reconciliation failed', err.getvalue())

    def test_running_the_script_as_main_invokes_main(self):
        argv = ['reconcile-firewall', '--interface', 'tailscale0', '--port', '22',
                '--incoming', 'deny', '--outgoing', 'allow']
        script = Path(__file__).with_name('reconcile-firewall.py')
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(Path, 'read_text', return_value='IPV6=maybe\n'), \
                mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as exit_error:
                runpy.run_path(str(script), run_name='__main__')
        self.assertEqual(exit_error.exception.code, 1)
        self.assertIn('Cannot determine UFW IPv6 policy', err.getvalue())


class GatewayReconciliationTests(unittest.TestCase):
    def test_matching_unit_is_preserved_and_drift_is_repaired(self):
        module = load_helper('ensure-gateway-unit')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'gateway.service'
            expected = '[Service]\nUser=hermes\nExecStart=/managed/python gateway\n'
            gateway = mock.Mock()
            gateway._hermes_home_pinned_by_unit.return_value = None
            gateway.get_systemd_unit_path.return_value = path
            gateway.generate_systemd_unit.return_value = expected
            gateway._normalize_service_definition.side_effect = lambda text: text.strip()
            gateway._strip_optional_systemd_directives.side_effect = lambda text: text
            gateway.systemd_install.side_effect = lambda **kwargs: path.write_text(expected)
            for prior in (None, expected, expected.replace('User=hermes', 'User=root')):
                with self.subTest(prior=prior):
                    if prior is not None:
                        path.write_text(prior)
                    gateway.systemd_install.reset_mock()
                    self.assertEqual(module.ensure_unit(gateway, 'hermes'), prior != expected)
                    self.assertEqual(gateway.systemd_install.called, prior != expected)
                    self.assertEqual(path.read_text(), expected)
                    gateway.generate_systemd_unit.assert_called_with(system=True, run_as_user='hermes')
            gateway.systemd_install.side_effect = None
            path.write_text('stale')
            with self.assertRaisesRegex(RuntimeError, 'verification'):
                module.ensure_unit(gateway, 'hermes')
            gateway._hermes_home_pinned_by_unit.return_value = '/another/home'
            gateway.get_hermes_home.return_value = Path(directory)
            gateway.systemd_install.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'another Hermes home'):
                module.ensure_unit(gateway, 'hermes')
            gateway.systemd_install.assert_not_called()


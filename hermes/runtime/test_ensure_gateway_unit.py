"""Behavioral checks for the pinned gateway systemd unit reconciliation helper."""
import importlib.util
import io
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock


MODULE_NAME = 'ensure-gateway-unit'


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnsureGatewayUnitTests(unittest.TestCase):
    def configured_gateway(self, path):
        """A gateway stand-in whose ensure_unit run reports a fresh installation."""
        gateway = mock.Mock()
        gateway.get_systemd_unit_path.return_value = path
        gateway.get_hermes_home.return_value = path.parent / 'home'
        gateway._hermes_home_pinned_by_unit.return_value = None
        gateway.generate_systemd_unit.return_value = '[Service]\nUser=hermes\n'
        gateway._normalize_service_definition.side_effect = lambda text: text.strip()
        gateway._strip_optional_systemd_directives.side_effect = lambda text: text
        gateway.systemd_install.side_effect = (
            lambda **kwargs: path.write_text(gateway.generate_systemd_unit.return_value))
        return gateway

    def test_symlinked_unit_path_is_refused(self):
        module = load_helper(MODULE_NAME)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'real.service'
            target.write_text('[Service]\nUser=other\n')
            link = Path(directory) / 'gateway.service'
            link.symlink_to(target)
            gateway = self.configured_gateway(link)
            with self.assertRaisesRegex(RuntimeError, 'must not be a symlink'):
                module.ensure_unit(gateway, 'hermes')
            gateway.systemd_install.assert_not_called()
            self.assertEqual(target.read_text(), '[Service]\nUser=other\n')

    def test_main_parses_user_reports_change_and_exits_zero(self):
        module = load_helper(MODULE_NAME)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'gateway.service'
            gateway = self.configured_gateway(path)
            output = io.StringIO()
            fake_cli = mock.Mock()
            fake_cli.gateway = gateway
            with mock.patch.dict(sys.modules, {'hermes_cli': fake_cli}), \
                    mock.patch.object(sys, 'argv', ['ensure-gateway-unit.py', '--user', 'hermes']), \
                    mock.patch.object(sys, 'stdout', output):
                module.main()
        self.assertEqual(output.getvalue().strip(), 'gateway-unit-changed=true')
        gateway.generate_systemd_unit.assert_called_once_with(system=True, run_as_user='hermes')
        gateway.systemd_install.assert_called_once()

    def test_running_the_script_as_main_invokes_main(self):
        script = Path(__file__).with_name(MODULE_NAME + '.py')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'gateway.service'
            gateway = self.configured_gateway(path)
            fake_cli = mock.Mock()
            fake_cli.gateway = gateway
            output = io.StringIO()
            with mock.patch.dict(sys.modules, {'hermes_cli': fake_cli}), \
                    mock.patch.object(sys, 'argv', ['ensure-gateway-unit.py', '--user', 'hermes']), \
                    mock.patch.object(sys, 'stdout', output):
                runpy.run_path(str(script), run_name='__main__')
        self.assertEqual(output.getvalue().strip(), 'gateway-unit-changed=true')
        gateway.systemd_install.assert_called_once()


if __name__ == '__main__':
    unittest.main()

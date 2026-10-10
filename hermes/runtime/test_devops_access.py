"""Execute the credential lookup documented in managed DevOps instructions."""

import contextlib
import io
import os
from pathlib import Path
import re
import sys
import types
import unittest
from unittest import mock


TEMPLATE = Path(__file__).resolve().parents[1] / 'ansible/templates/devops-access.md.j2'


class DevopsAccessTests(unittest.TestCase):
    def lookup(self):
        blocks = re.findall(r'```python\n(.*?)\n```', TEMPLATE.read_text(), re.DOTALL)
        self.assertEqual(len(blocks), 1, 'managed instructions must include executable credential lookup')
        namespace = {}
        exec(compile(blocks[0], str(TEMPLATE), 'exec'), namespace)
        return namespace['integration_token']

    def test_process_credential_wins_without_reading_a_file(self):
        dotenv = types.ModuleType('dotenv')
        dotenv.dotenv_values = mock.Mock(side_effect=AssertionError('must not read file'))
        with mock.patch.dict(sys.modules, {'dotenv': dotenv}), \
                mock.patch.dict(os.environ, {'SONAR_TOKEN': 'fixture-process-token'}, clear=True):
            self.assertEqual(self.lookup()('SONAR_TOKEN'), 'fixture-process-token')
        dotenv.dotenv_values.assert_not_called()

    def test_invalid_header_credentials_are_rejected_without_echoing_values(self):
        for source in ('process', 'file'):
            for value in ('fixture-private\nvalue', 'fixture-private\x7fvalue'):
                with self.subTest(source=source, character=ord(value[15])):
                    dotenv = types.ModuleType('dotenv')
                    dotenv.dotenv_values = mock.Mock(return_value={'SONAR_TOKEN': value})
                    env = {'SONAR_TOKEN': value} if source == 'process' else {}
                    with mock.patch.dict(sys.modules, {'dotenv': dotenv}), \
                            mock.patch.dict(os.environ, env, clear=True):
                        lookup = self.lookup()
                        with self.assertRaisesRegex(RuntimeError, 'invalid credential format') as caught:
                            lookup('SONAR_TOKEN')
                    self.assertNotIn('fixture-private', str(caught.exception))

    def test_missing_process_credential_uses_managed_home_without_interpolation_or_output(self):
        dotenv = types.ModuleType('dotenv')
        dotenv.dotenv_values = mock.Mock(return_value={'SONAR_TOKEN': 'fixture-file-token',
                                                     'UNRELATED_SECRET': 'fixture-other-token'})
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {'dotenv': dotenv}), \
                mock.patch.dict(os.environ, {'HERMES_HOME': '/srv/hermes-profile'}, clear=True), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(self.lookup()('SONAR_TOKEN'), 'fixture-file-token')
        dotenv.dotenv_values.assert_called_once_with(Path('/srv/hermes-profile/.env'), interpolate=False)
        self.assertEqual(output.getvalue(), '')

    def test_missing_and_unreadable_sources_remain_distinct(self):
        for values in ({}, {'SONAR_TOKEN': None}, PermissionError('fixture-private-detail')):
            with self.subTest(values=type(values).__name__):
                dotenv = types.ModuleType('dotenv')
                dotenv.dotenv_values = mock.Mock()
                if isinstance(values, Exception):
                    dotenv.dotenv_values.side_effect = values
                else:
                    dotenv.dotenv_values.return_value = values
                with mock.patch.dict(sys.modules, {'dotenv': dotenv}), \
                        mock.patch.dict(os.environ, {'HERMES_HOME': '/srv/hermes-profile'}, clear=True):
                    lookup = self.lookup()
                    if isinstance(values, Exception):
                        with self.assertRaisesRegex(RuntimeError, 'managed environment file is unreadable'):
                            lookup('SONAR_TOKEN')
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'absent from process and managed environment file'):
                            lookup('SONAR_TOKEN')


if __name__ == '__main__':
    unittest.main()

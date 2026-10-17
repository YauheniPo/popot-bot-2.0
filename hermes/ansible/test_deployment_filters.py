"""Checks for deployment state comparisons, including private-only Serve."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest


class DeploymentFilterTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parent / 'filter_plugins/deployment_state.py'
        spec = importlib.util.spec_from_file_location('deployment_state', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.services = [{'protocol': 'https', 'port': 443, 'target': 'http://127.0.0.1:9119'}]
        self.state = {'TCP': {'443': {'HTTPS': True}},
                      'Web': {'host.example.ts.net:443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:9119'}}}}}

    def test_matching_private_serve_configuration_is_unchanged(self):
        self.assertTrue(self.module.serve_matches(self.state, 'host.example.ts.net.', self.services))
        self.state['AllowFunnel'] = {'host.example.ts.net:443': False}
        self.assertTrue(self.module.serve_matches(self.state, 'host.example.ts.net', self.services))

    def test_stale_routes_wrong_protocol_host_and_public_funnel_require_repair(self):
        variants = [None, {}, {**self.state, 'AllowFunnel': {'host.example.ts.net:443': True}},
                    {**self.state, 'TCP': {'443': {'HTTP': True}}},
                    {**self.state, 'TCP': {**self.state['TCP'], '22': {'TCPForward': 'localhost:22'}}}]
        extra_path = deepcopy(self.state)
        extra_path['Web']['host.example.ts.net:443']['Handlers']['/extra'] = {'Text': 'stale'}
        variants.append(extra_path)
        for state in variants:
            with self.subTest(state=state):
                self.assertFalse(self.module.serve_matches(state, 'host.example.ts.net', self.services))
        self.assertFalse(self.module.serve_matches(self.state, 'new.example.ts.net', self.services))

    def test_bad_state_or_missing_dns_must_not_authorize_a_reset(self):
        for state, dns in (([], 'host.example.ts.net'), (self.state, '')):
            with self.subTest(state=state, dns=dns):
                with self.assertRaises(ValueError):
                    self.module.serve_matches(state, dns, self.services)

    def test_runtime_capability_keeps_its_current_value_until_native_validation(self):
        candidate = {'web': {'search_backend': 'auto', 'extract_backend': 'auto'}, 'personal': True}
        existing = {'web': {'search_backend': 'searxng'}}
        runtime = {'capabilities': {'search': {'set': {'web.search_backend': 'searxng'}}}}
        result = self.module.preserve_runtime_capabilities(candidate, existing, runtime, ['search'])
        self.assertEqual(result['web'], {'search_backend': 'searxng', 'extract_backend': 'auto'})
        self.assertEqual(candidate['web']['search_backend'], 'auto')
        self.assertEqual(self.module.preserve_runtime_capabilities(candidate, existing, runtime, []), candidate)
        missing = self.module.preserve_runtime_capabilities(candidate, {}, runtime, ['search'])
        self.assertNotIn('search_backend', missing['web'])


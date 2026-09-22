"""Native integration contracts without credentials, model requests or gateway restarts."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).parent / 'team-workflow'
SPEC = importlib.util.spec_from_file_location('team_workflow_plugin', SOURCE / '__init__.py',
                                             submodule_search_locations=[str(SOURCE)])
PLUGIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLUGIN
SPEC.loader.exec_module(PLUGIN)


def module(name, **attributes):
    result = types.ModuleType(name)
    result.__dict__.update(attributes)
    return result


class NativeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.state = self.root / 'team-workflow'
        self.state.mkdir()
        self.parent = types.SimpleNamespace(_delegate_depth=0)
        self.backend = PLUGIN.NativeBackend(self.parent, 'owner', self.state, self.workspace)
        self.delegate = Mock(return_value=json.dumps({'status': 'dispatched', 'delegation_id': 'native'}))
        self.extract = Mock(side_effect=lambda text: text.removeprefix('```json\n').removesuffix('\n```'))
        self.lookup = Mock()
        self.config = {'terminal': {'cwd': str(self.workspace)}, 'team_workflow': {
            'enabled': True, 'max_revision_rounds': 1, 'deadline_seconds': 300}}
        self.modules = {
            'tools.delegate_tool': module('tools.delegate_tool', delegate_task=self.delegate),
            'tools.async_delegation': module('tools.async_delegation', get_durable_delegation=self.lookup),
            'tools.delegation_output_schema': module('tools.delegation_output_schema', extract_json_candidate=self.extract),
            'agent.subagent_lifecycle': module('agent.subagent_lifecycle', get_active_subagent_parent=lambda: self.parent),
            'hermes_constants': module('hermes_constants', get_hermes_home=lambda: self.root),
            'hermes_cli.config': module('hermes_cli.config', load_config=lambda: self.config),
            'tools.approval': module('tools.approval', get_current_session_key=lambda default: 'owner'),
            'gateway.session_context': module('gateway.session_context', async_delivery_supported=lambda: True),
        }

    def test_dispatch_uses_native_schema_parent_and_model_route(self):
        with patch.dict(sys.modules, self.modules):
            self.assertEqual(self.backend.spawn('brief', {'type': 'object'})['status'], 'dispatched')
        self.delegate.assert_called_once_with(goal='brief', output_schema={'type': 'object'},
                                              role='leaf', background=True, parent_agent=self.parent)

    def test_native_json_extraction_preserves_durable_record(self):
        original = {'origin_session': 'owner', 'state': 'completed', 'result': {
            'results': [{'summary': '```json\n{"status":"done"}\n```', 'schema_valid': True}]}}
        self.lookup.return_value = original
        with patch.dict(sys.modules, self.modules):
            record = self.backend.result('native')
        self.assertEqual(json.loads(record['result']['results'][0]['summary'])['status'], 'done')
        self.assertTrue(original['result']['results'][0]['summary'].startswith('```'))

    def test_cancel_targets_only_supplied_native_children(self):
        with patch.dict(sys.modules, self.modules):
            self.backend.stop(['child-a', 'child-b'])
        self.assertEqual([call.kwargs['subagent_id'] for call in self.delegate.call_args_list], ['child-a', 'child-b'])
        self.assertTrue(all(call.kwargs['action'] == 'stop' for call in self.delegate.call_args_list))

    def test_leaf_and_unroutable_sessions_cannot_start_a_team(self):
        with patch.dict(sys.modules, self.modules):
            self.parent._delegate_depth = 1
            self.assertIn('main coordinator', json.loads(PLUGIN.handle({'action': 'start'}))['error'])
            self.parent._delegate_depth = 0
            self.modules['gateway.session_context'].async_delivery_supported = lambda: False
            self.assertIn('routable', json.loads(PLUGIN.handle({'action': 'start'}))['error'])
        self.delegate.assert_not_called()

    def test_handler_enforces_disabled_policy_and_registers_tool(self):
        self.config['team_workflow']['enabled'] = False
        with patch.dict(sys.modules, self.modules):
            self.assertIn('disabled', json.loads(PLUGIN.handle({'action': 'start'}))['error'])
        context = Mock()
        PLUGIN.register(context)
        definition = context.register_tool.call_args.kwargs
        self.assertIs(definition['handler'], PLUGIN.handle)
        self.assertEqual(definition['toolset'], 'delegation')
        self.assertEqual(definition['schema']['parameters']['required'], ['action'])
        self.delegate.assert_not_called()

    def test_code_worktree_uses_committed_head_without_changing_source(self):
        repo = self.workspace / 'repo'
        repo.mkdir()

        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()

        git('init')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'fixture')
        before = git('rev-parse', 'HEAD')
        (repo / 'local-only.txt').write_text('preserve user changes')
        worktree = Path(self.backend.prepare_worktree('a' * 32, str(repo)))
        self.assertTrue(worktree.is_dir())
        self.assertFalse((worktree / 'local-only.txt').exists())
        self.assertEqual((repo / 'local-only.txt').read_text(), 'preserve user changes')
        self.assertEqual(git('rev-parse', 'HEAD'), before)
        with self.assertRaisesRegex(ValueError, 'inside'):
            self.backend.prepare_worktree('b' * 32, str(self.root))
        with self.assertRaisesRegex(ValueError, 'absolute'):
            self.backend.prepare_worktree('b' * 32, '../repo')

    def test_worktree_identity_and_unsafe_target_are_rejected(self):
        repo = self.workspace / 'repo'
        repo.mkdir()
        subprocess.run(['git', '-C', str(repo), 'init'], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, 'Invalid task identity'):
            self.backend.prepare_worktree('not-a-team-id', str(repo))
        with self.assertRaisesRegex(ValueError, 'Invalid task identity'):
            self.backend.prepare_worktree('A' * 32, str(repo))
        self.assertFalse((self.state / 'worktrees').exists())
        linked = self.root / 'linked-worktrees'
        linked.mkdir()
        worktrees = self.state / 'worktrees'
        worktrees.symlink_to(linked)
        with self.assertRaisesRegex(ValueError, 'Worktree target already exists or is unsafe'):
            self.backend.prepare_worktree('d' * 32, str(repo))
        self.assertEqual(list(linked.iterdir()), [])
        worktrees.unlink()
        existing = worktrees / ('c' * 32)
        existing.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'Worktree target already exists or is unsafe'):
            self.backend.prepare_worktree('c' * 32, str(repo))
        self.assertEqual(list(existing.iterdir()), [])

    def test_handler_dispatches_native_work_and_returns_json(self):
        with patch.dict(sys.modules, self.modules):
            self.assertEqual(json.loads(PLUGIN.handle({'action': 'list'})), [])
            started = json.loads(PLUGIN.handle({
                'action': 'start', 'request_id': 'adapter', 'objective': 'Inspect the fixture service',
                'mode': 'research', 'acceptance': 'Cites primary sources', 'context': 'Read-only access'}))
        self.assertEqual(started['status'], 'running')
        self.assertEqual(started['stage'], 'research')
        self.assertEqual(started['mode'], 'research')
        self.assertIn('Inspect the fixture service', self.delegate.call_args.kwargs['goal'])
        self.assertEqual(self.delegate.call_args.kwargs['role'], 'leaf')
        self.assertTrue(self.delegate.call_args.kwargs['background'])

    def test_terminal_cwd_must_be_absolute_for_team_tasks(self):
        for cwd in ('relative/workspace', ''):
            with self.subTest(cwd=cwd):
                self.config['terminal'] = {'cwd': cwd}
                with patch.dict(sys.modules, self.modules):
                    error = json.loads(PLUGIN.handle({'action': 'list'}))['error']
                self.assertEqual(error, 'Configure an absolute terminal.cwd for team tasks')
        self.delegate.assert_not_called()

    def test_unexpected_controller_failure_never_replays_or_leaks_details(self):
        self.modules['hermes_cli.config'] = module(
            'hermes_cli.config', load_config=Mock(side_effect=RuntimeError('provider stderr with a credential')))
        with patch.dict(sys.modules, self.modules):
            result = json.loads(PLUGIN.handle({'action': 'start'}))
        self.assertEqual(result, {'error': 'Team controller failed; no automatic replay',
                                  'type': 'RuntimeError'})
        self.delegate.assert_not_called()


if __name__ == '__main__':
    unittest.main()

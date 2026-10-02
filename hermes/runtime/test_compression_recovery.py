"""Compression timeouts preserve gateway sessions and use configured fallback routes."""

import ast
import asyncio
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    'compression_patches', Path(__file__).with_name('apply-hermes-patches.py'))
PATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)


def patch_block(label):
    return next(new for _, marker, _, new in PATCHER._PATCHES
                if marker == '# Local Hermes: ' + label)


class CompressionRecoveryTests(unittest.TestCase):
    def test_timeout_reason_survives_gateway_result_mapping(self):
        code = 'common = {\n' + patch_block('compression exit reason') + '\n}'
        for reason in ('context_compression_timeout', 'context_compression_exhausted', None):
            ns = {'result': {'compression_exhausted': True, 'turn_exit_reason': reason}}
            exec(code, ns)
            self.assertEqual(ns['common']['turn_exit_reason'], reason)
            self.assertTrue(ns['common']['compression_exhausted'])

    def test_timeout_keeps_session_and_gives_compress_guidance(self):
        ns = {'logger': mock.Mock()}
        code = textwrap.dedent(patch_block('preserve session on compression timeout'))
        # The original deferred branch remains below the inserted timeout guard.
        code += '    return response, session_entry\nreturn "normal reset path", session_entry\n'
        exec('async def handle(agent_result, response, session_entry):\n'
             + textwrap.indent(code, '    '), ns)
        entry = object()
        result = {'compression_exhausted': True, 'turn_exit_reason': 'context_compression_timeout'}
        response, returned = asyncio.run(ns['handle'](result, 'old /new guidance', entry))
        self.assertIs(returned, entry)
        self.assertIn('/compress', response)
        self.assertNotIn('/new', response)
        self.assertNotIn('auto-reset', response)
        self.assertEqual(asyncio.run(ns['handle'](
            {'compression_exhausted': True}, 'original', entry))[0], 'normal reset path')
        self.assertEqual(asyncio.run(ns['handle'](
            {'compression_deferred': True}, 'original', entry))[0], 'original')

    def test_failed_or_interrupted_turn_does_not_prepend_reasoning(self):
        ns = {}
        code = textwrap.dedent(patch_block('hide reasoning on unsuccessful turns'))
        exec('def display(agent_result, response):\n' + textwrap.indent(code, '    ')
             + '    return last_reasoning\n', ns)
        for flag, value in (('failed', True), ('error', 'timeout'), ('interrupted', True),
                            ('compression_exhausted', True), ('compression_deferred', True)):
            with self.subTest(flag=flag):
                result = {'last_reasoning': 'unfinished speculation', flag: value}
                self.assertEqual(ns['display'](result, 'failure report'), 'failure report')
        self.assertEqual(ns['display']({'last_reasoning': 'success'}, 'answer'), 'success')

    def test_compression_inherits_main_chain_only_when_no_explicit_override(self):
        inherited = (object(), 'test:free', 'fallback_providers[0](openrouter)')
        main_chain = mock.Mock(return_value=inherited)
        config = mock.Mock(return_value={})
        ns = {'_get_auxiliary_task_config': config, '_try_main_fallback_chain': main_chain}
        code = textwrap.dedent(patch_block('compression uses configured fallback'))
        exec('def select(task, failed_provider, reason, failed_model, failed_base_url, failure_scope):\n'
             + textwrap.indent(code, '    ') + '    return chain\n', ns)
        scope = object()
        args = ('compression', 'nvidia', 'timeout', 'primary', 'endpoint', scope)
        self.assertIs(ns['select'](*args), inherited)
        main_chain.assert_called_once_with('compression', 'nvidia', reason='timeout',
                                          failed_model='primary', failed_base_url='endpoint',
                                          failure_scope=scope)
        main_chain.reset_mock()
        for override in ([], None, [{'provider': 'nous', 'model': 'chosen'}], 'invalid'):
            with self.subTest(override=override):
                config.return_value = {'fallback_chain': override}
                self.assertIs(ns['select'](*args), override)
        config.return_value = {}
        self.assertIsNone(ns['select']('vision', *args[1:]))
        main_chain.assert_not_called()

    def test_compression_fallback_rejects_paid_openrouter(self):
        ns = {'skip': lambda *_: False, 'tried': []}
        code = textwrap.dedent(patch_block('compression fallback free OpenRouter'))
        # The native loop appends a skip diagnostic and continues below the anchor.
        code += '    continue\nreturn True\n'
        exec('def eligible(task, fb_norm, fb_model):\n'
             '    fb_provider, fb_base_url = fb_norm, ""\n'
             '    for _ in range(1):\n' + textwrap.indent(code, '        ')
             + '    return False\n', ns)
        self.assertFalse(ns['eligible']('compression', 'openrouter', 'paid'))
        self.assertTrue(ns['eligible']('compression', 'openrouter', 'model:free'))
        self.assertTrue(ns['eligible']('compression', 'nvidia', 'model'))
        self.assertTrue(ns['eligible']('vision', 'openrouter', 'existing-policy'))


@unittest.skipUnless(os.environ.get('HERMES_UPSTREAM_DIR'), 'Pinned upstream source required')
class CompressionRecoveryUpstreamTests(unittest.TestCase):
    def source(self, path):
        source = (Path(os.environ['HERMES_UPSTREAM_DIR']) / path).read_text()
        for target, marker, old, new in PATCHER._PATCHES:
            if target == path:
                self.assertEqual(source.count(old), 1, marker)
                source = source.replace(old, new, 1)
        ast.parse(source)
        return source

    def method(self, path, name, ns):
        node = next(n for n in ast.walk(ast.parse(self.source(path)))
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
        exec('from __future__ import annotations\n' + ast.unparse(node), ns)
        return ns[name]

    def test_real_reset_handler_preserves_timeout_and_resets_structural_exhaustion(self):
        ns = {'asyncio': asyncio, 'logger': mock.Mock()}
        handle = self.method('gateway/run_turn.py', '_hmwa_compression_exhaustion_reset', ns)
        old, new, source = (SimpleNamespace(session_id=key) for key in ('old', 'new', 'source'))
        runner = SimpleNamespace(async_session_store=SimpleNamespace(reset_session=mock.AsyncMock(return_value=new)),
                                 _evict_cached_agent=mock.Mock(), _clear_conversation_scope=mock.Mock(),
                                 _sync_telegram_topic_binding=mock.Mock())
        for result in ({'compression_exhausted': True, 'turn_exit_reason': 'context_compression_timeout'},
                       {'compression_deferred': True, 'compression_exhausted': True}, {}):
            with self.subTest(result=result):
                _, entry = asyncio.run(handle(runner, result, 'response', old, 'key', source))
                self.assertIs(entry, old)
                runner.async_session_store.reset_session.assert_not_awaited()
                runner._evict_cached_agent.assert_not_called()
                runner._clear_conversation_scope.assert_not_called()
                runner._sync_telegram_topic_binding.assert_not_called()
        response, entry = asyncio.run(handle(runner, {'compression_exhausted': True},
                                             'response', old, 'key', source))
        self.assertIs(entry, new)
        self.assertIn('auto-reset', response)
        runner.async_session_store.reset_session.assert_awaited_once_with('key')
        runner._sync_telegram_topic_binding.assert_called_once_with(
            source, new, reason='compression-exhausted-reset')

    def test_all_touched_upstream_files_parse(self):
        for path in ('gateway/run_turn.py', 'gateway/run_turn_runner.py', 'agent/auxiliary_client.py'):
            with self.subTest(path=path):
                self.source(path)

    def test_inherited_chain_skips_failed_paid_unavailable_and_small_routes(self):
        routes = [{'provider': provider, 'model': model} for provider, model in (
            ('openrouter', 'paid'), ('nvidia', 'primary'), ('nous', 'unavailable'),
            ('openrouter', 'small:free'), ('openrouter', 'working:free'))]
        client = object()
        resolve = mock.Mock(side_effect=[(None, None), (client, 'small:free'), (client, 'working:free')])
        ns = {
            'logger': mock.Mock(), '_get_auxiliary_task_config': lambda _: {},
            '_failed_backend_skip': lambda *_a, **_k: lambda p, m, _: (p, m) == ('nvidia', 'primary'),
            '_task_minimum_context_length': lambda _: 100,
            '_custom_health_base_url': lambda *_: '',
            '_is_provider_unhealthy': lambda *_: False,
            '_resolve_fallback_entry': resolve,
            '_context_too_small': lambda entry, *_a, **_k: entry['model'] == 'small:free',
        }
        self.method('agent/auxiliary_client.py', '_try_main_fallback_chain', ns)
        select = self.method('agent/auxiliary_client.py', '_try_configured_fallback_chain', ns)
        modules = {
            'hermes_cli.config': SimpleNamespace(load_config_readonly=lambda: {'fallback_providers': routes}),
            'hermes_cli.fallback_config': SimpleNamespace(get_fallback_chain=lambda c: c['fallback_providers']),
        }
        with mock.patch.dict('sys.modules', modules):
            self.assertEqual(select('compression', 'nvidia', failed_model='primary'),
                             (client, 'working:free', 'openrouter'))
            self.assertEqual(resolve.call_args_list, [mock.call(route) for route in routes[2:]])
            # /fallback off takes effect on the next read, without managed defaults being restored.
            routes.clear()
            resolve.reset_mock()
            self.assertEqual(select('compression', 'nvidia'), (None, None, ''))
            resolve.assert_not_called()

    def test_touched_patches_apply_and_repeat_without_writes(self):
        paths = {'gateway/run_turn.py', 'gateway/run_turn_runner.py', 'agent/auxiliary_client.py'}
        selected = [patch for patch in PATCHER._PATCHES if patch[0] in paths]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in paths:
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((Path(os.environ['HERMES_UPSTREAM_DIR']) / path).read_bytes())
            state = {}
            with mock.patch.object(PATCHER, 'HERMES_AGENT_DIR', root), contextlib.redirect_stdout(io.StringIO()):
                for patch in selected:
                    self.assertEqual(PATCHER._apply_one_patch(*patch, state)[:2], (1, None))
                first = {path: (root / path).read_bytes() for path in paths}
                for patch in selected:
                    self.assertEqual(PATCHER._apply_one_patch(*patch, state)[:2], (0, None))
                self.assertEqual(first, {path: (root / path).read_bytes() for path in paths})


if __name__ == '__main__':
    unittest.main()

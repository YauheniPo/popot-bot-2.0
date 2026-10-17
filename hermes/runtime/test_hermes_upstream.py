"""Opt-in compatibility checks against an unmodified, pinned Hermes checkout.

Set HERMES_UPSTREAM_DIR to a local upstream checkout. These tests do not
download/install dependencies, use credentials, or contact a deployed gateway.
"""

import ast
import asyncio
from collections.abc import Mapping
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import logging
import re
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import tomllib
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock
import zipfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("upstream_patches", ROOT / "runtime/apply-hermes-patches.py")
patches = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patches)
UPSTREAM = os.environ.get("HERMES_UPSTREAM_DIR")


@unittest.skipUnless(UPSTREAM, "HERMES_UPSTREAM_DIR is not configured")
class HermesUpstreamTests(unittest.TestCase):
    def test_managed_verification_skill_loads_through_native_autoload(self):
        # Use the real pinned skill resolver and external directory, without a
        # model call. Only the VPS platform is simulated on non-Linux hosts.
        script = '''
import os, sys, tempfile
from pathlib import Path
from unittest import mock
import yaml
root = Path(sys.argv[2])
settings = yaml.safe_load((root / 'config/vps-defaults.yml').read_text())
with tempfile.TemporaryDirectory() as directory:
    os.environ['HERMES_HOME'] = directory
    config = {'skills': {
        'auto_load': settings['vps_runtime']['set_if_missing']['skills.auto_load'],
        'external_dirs': [str(root / 'skills')],
    }}
    Path(directory, 'config.yaml').write_text(yaml.safe_dump(config))
    sys.path.insert(0, sys.argv[1])
    from agent.skill_commands import build_auto_load_prompt
    with mock.patch('agent.skill_utils.sys.platform', 'linux'):
        prompt, loaded, missing = build_auto_load_prompt(
            user_config=config, home_override=Path(directory))
        assert 'task_verification' in loaded, (loaded, missing)
        assert not missing, missing
        assert 'references/diagnosing-failures.md' in prompt
        config['skills']['auto_load'] = []
        assert build_auto_load_prompt(user_config=config, home_override=Path(directory)) == ('', [], [])
'''
        result = subprocess.run(
            [sys.executable, '-c', script, str(UPSTREAM), str(ROOT)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_endpoint_404_is_terminal_but_specific_error_reasons_are_preserved(self):
        classifier = ModuleType('endpoint_classifier_fixture')
        with mock.patch.dict(sys.modules, {classifier.__name__: classifier}):
            exec(self.patched_source('agent/error_classifier.py'), classifier.__dict__)
        for message, code, reason in (
            ('page not found', '', 'unknown'),
            ('Not Found', 'insufficient_quota', 'billing'),
            ('model not found', '', 'model_not_found'),
        ):
            with self.subTest(message=message):
                context = SimpleNamespace(msg=message, code=code, model_slug='fixture', provider_slug='fixture')
                with mock.patch.object(classifier, '_model_id_missing_known_prefix', return_value=False):
                    verdict = classifier._status_404(context)
                self.assertEqual(verdict['reason'].value, reason)
                self.assertFalse(verdict.get('retryable', False))
                self.assertTrue(verdict['should_fallback'])
                self.assertNotIn(verdict['reason'], classifier.RETRYABLE_CLIENT_REASONS)
        with mock.patch.object(classifier, '_model_id_missing_known_prefix', return_value=True):
            context = SimpleNamespace(msg='not found', code='', model_slug='fixture', provider_slug='fixture')
            self.assertEqual(classifier._status_404(context), classifier._V_MODEL_NOT_FOUND)

    def test_fallback_observer_is_best_effort_and_runs_after_route_initialization(self):
        patch = next(p for p in patches._PATCHES if p[1] == '# Local Hermes: confirmed fallback telemetry')
        source = 'def activate():\n' + textwrap.indent(textwrap.dedent(patch[3]), '    ')
        agent = SimpleNamespace(model='new', base_url='endpoint', session_id='session')
        namespace = dict(agent=agent, fb_provider='new-provider', fb_model='new',
                         old_provider='old-provider', old_model='old', logger=mock.Mock())
        resolve = namespace['resolve_native_compaction_capabilities'] = mock.Mock(return_value='capabilities')
        exec(source, namespace)
        for available, error in ((False, None), (True, None), (True, RuntimeError('observer unavailable'))):
            invoke = mock.Mock(side_effect=error)
            with mock.patch.dict(sys.modules, {'hermes_cli.lifecycle': SimpleNamespace(
                    has_hook=lambda _: available, invoke_hook=invoke)}):
                self.assertTrue(namespace['activate']())
            self.assertEqual(invoke.call_count, int(available))
            self.assertEqual(agent.runtime_capabilities, 'capabilities')
        resolve.side_effect = RuntimeError('route initialization failed')
        with mock.patch.dict(sys.modules, {'hermes_cli.lifecycle': SimpleNamespace(
                has_hook=lambda _: True, invoke_hook=invoke)}):
            invoke.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'route initialization failed'):
                namespace['activate']()
            invoke.assert_not_called()

    def test_native_skill_lifecycle_reaches_observer_with_turn_context(self):
        # Execute native usage persistence and ContextVars in an isolated process;
        # no AIAgent, provider requests, or installed runtime modifications.
        script = '''
import importlib.util, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
with tempfile.TemporaryDirectory() as directory:
    os.environ['HERMES_HOME'] = directory
    sys.path.insert(0, sys.argv[1])
    from tools import skill_usage, skill_provenance
    from agent.subagent_lifecycle import bind_subagent_parent
    from hermes_cli import lifecycle
    spec = importlib.util.spec_from_file_location('skill_observer', sys.argv[2])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    database = plugin._db()
    def invoke(name, **kwargs):
        assert name == 'on_skill_lifecycle'
        plugin._on_skill_lifecycle(**kwargs)
    with mock.patch.object(lifecycle, 'has_hook', return_value=True), \
         mock.patch.object(lifecycle, 'invoke_hook', side_effect=invoke), \
         mock.patch.object(plugin.hooks, '_execute', side_effect=database.execute), \
         mock.patch.object(plugin.hooks, '_audit'):
        for depth in (0, 1):
            with bind_subagent_parent(SimpleNamespace(session_id='session-' + str(depth), _delegate_depth=depth)):
                skill_usage.bump_use('demo', task_id='task')
        with bind_subagent_parent(SimpleNamespace(session_id='session-0')):
            token = skill_provenance.set_current_write_origin('background_review')
            try:
                skill_usage.bump_patch('demo', task_id='task')
            finally:
                skill_provenance.reset_current_write_origin(token)
        skill_usage.bump_use('demo', task_id='cron-job')  # preload before a turn
        with mock.patch.object(skill_usage, 'save_usage', return_value=False):
            skill_usage.bump_patch('demo')
    rows = database.execute('SELECT action, agent_type, session_id FROM skill_events ORDER BY rowid').fetchall()
    assert rows == [('loaded', 'main', 'session-0'), ('loaded', 'subagent', 'session-1'),
                    ('patched', 'self_improvement', 'session-0'), ('loaded', 'unknown', '')], rows
    database.close()
'''
        result = subprocess.run(
            [sys.executable, '-c', script, str(UPSTREAM),
             str(ROOT / 'ops/plugin/ops-observability/__init__.py')],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_chat_switch_syncs_all_cron_only_after_success_and_never_for_once(self):
        source = self.patched_source('gateway/slash_commands_model.py')
        tree = ast.parse(source)
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == '_commit_model_switch_locked')
        namespace = {'asyncio': asyncio, 'logger': mock.Mock()}
        exec('from __future__ import annotations\n' + ast.unparse(
            ast.Module(body=[method], type_ignores=[])), namespace)
        for once, picker, switch_error, persist_error in (
            (False, False, None, None), (False, True, None, None),
            (True, False, None, None), (False, False, 'switch failed', None),
            (False, False, None, OSError('private diagnostic')),
        ):
            with self.subTest(once=once, picker=picker, failed=bool(switch_error or persist_error)):
                runner = SimpleNamespace(
                    _switch_cached_agent_model=mock.Mock(return_value=switch_error),
                    _record_model_switch=mock.AsyncMock(return_value=None),
                    _model_switch_confirmation=mock.AsyncMock(return_value='Chat model changed'))
                context = SimpleNamespace(one_turn=once, reasoning_effort='')
                selection = SimpleNamespace(new_model='selected', target_provider='provider')
                persist = mock.Mock(return_value=True, side_effect=persist_error)
                with mock.patch.dict(sys.modules, {'cron.shared_model': SimpleNamespace(persist_chat_model=persist)}):
                    reply = asyncio.run(namespace['_commit_model_switch_locked'](
                        runner, selection, context, source=None, picker=picker))
                if once or switch_error:
                    persist.assert_not_called()
                else:
                    persist.assert_called_once_with(selection)
                    if persist_error:
                        self.assertIn('Cron model was not updated', reply)
                        self.assertNotIn('private diagnostic', reply)
                    else:
                        self.assertIn('all agent cron jobs', reply)

    def test_cli_cron_sync_requires_a_successful_permanent_choice(self):
        tree = ast.parse(self.patched_source('hermes_cli/cli_model_switch_mixin.py'))
        method = next(node for node in tree.body if getattr(node, 'name', '') == '_commit_model_switch')
        ns = {'_print_switch_summary': mock.Mock()}
        exec('from __future__ import annotations\n' + ast.unparse(ast.Module(body=[method], type_ignores=[])), ns)
        for once, swapped, saved in ((False, True, True), (True, True, True),
                                     (False, False, True), (False, True, False)):
            with self.subTest(once=once, swapped=swapped, saved=saved):
                cli = mock.Mock(model='old')
                cli._stage_and_swap_model.return_value = swapped
                persist = mock.Mock(return_value=True, side_effect=None if saved else OSError('private detail'))
                display = mock.Mock()
                selection = object()
                with mock.patch.dict(sys.modules, {
                    'cli': SimpleNamespace(HermesCLI=mock.Mock(), _cprint=display, logger=mock.Mock()),
                    'cron.shared_model': SimpleNamespace(persist_chat_model=persist),
                }):
                    ns['_commit_model_switch'](cli, selection, persist_global=False, one_turn=once)
                if once or not swapped:
                    persist.assert_not_called()
                else:
                    persist.assert_called_once_with(selection)
                    self.assertIn('all agent cron jobs' if saved else 'Cron model was not updated',
                                  str(display.call_args_list))
                    self.assertNotIn('private detail', str(display.call_args_list))

    def test_tui_cron_sync_ignores_once_automatic_adoption_and_failed_switches(self):
        tree = ast.parse(self.patched_source('tui_gateway/model_switch.py'))
        method = next(node for node in tree.body if getattr(node, 'name', '') == '_apply_model_switch')
        for once, pin, swapped, saved in ((False, True, True, True), (True, True, True, True),
                                         (False, False, True, True), (False, True, False, True),
                                         (False, True, True, False)):
            with self.subTest(once=once, pin=pin, swapped=swapped, saved=saved):
                ns = {'contextlib': contextlib, 'logger': mock.Mock(),
                      '_switch_request': lambda *a: ('selected', 'provider', once, False, ''),
                      '_current_model_runtime': lambda *a: ('old-provider', 'old', '', ''),
                      '_snapshot_agent_model_runtime': mock.Mock(), '_merge_preflight_warning': mock.Mock(),
                      '_commit_agent_switch': mock.Mock(side_effect=None if swapped else ValueError('failed'))}
                exec('from __future__ import annotations\n' + ast.unparse(
                    ast.Module(body=[method], type_ignores=[])), ns)
                selection = SimpleNamespace(success=True, new_model='selected', target_provider='provider',
                                            base_url='', api_key='private-key', api_mode='chat', warning_message='')
                persist = mock.Mock(return_value=True, side_effect=None if saved else OSError('private detail'))
                with mock.patch.dict(sys.modules, {
                    'hermes_cli.model_switch': SimpleNamespace(switch_model=lambda **kw: selection),
                    'hermes_cli.config': SimpleNamespace(load_config=lambda: {},
                                                        get_compatible_custom_providers=lambda cfg: []),
                    'cron.shared_model': SimpleNamespace(persist_chat_model=persist),
                }):
                    call = ns['_apply_model_switch']
                    session = {'agent': mock.Mock()}
                    kwargs = {'confirm_expensive_model': True, 'pin_session_override': pin}
                    if swapped:
                        response = call('session', session, 'selected', **kwargs)
                    else:
                        with self.assertRaisesRegex(ValueError, 'failed'):
                            call('session', session, 'selected', **kwargs)
                if once or not pin or not swapped:
                    persist.assert_not_called()
                else:
                    persist.assert_called_once_with(selection)
                    self.assertIn('all agent cron jobs' if saved else 'Cron model was not updated', response['warning'])
                    self.assertNotIn('private detail', response['warning'])

    def test_managed_cron_model_migration_preserves_native_job_records(self):
        # A separate interpreter keeps the real upstream imports/profile globals
        # isolated from the AST fixtures in the other compatibility tests.
        script = '''
import copy, importlib.util, os, sys, tempfile
from pathlib import Path
spec = importlib.util.spec_from_file_location('configurator', sys.argv[1])
configurator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configurator)
with tempfile.TemporaryDirectory() as directory:
    home = Path(directory)
    os.environ['HERMES_HOME'] = directory
    (home / 'hermes-agent').symlink_to(sys.argv[2])
    jobs = configurator._load_cron_store(home)
    homes = (home, home / 'profiles/builder')
    before = {}
    for location in homes:
        location.mkdir(parents=True, exist_ok=True)
        with jobs.use_cron_store(location):
            jobs.create_job(prompt='fixture', schedule='every 1h', provider='old', model='old',
                            base_url='https://old.invalid', deliver='telegram:123')
            jobs.create_job(prompt='paused', schedule='every 1h', provider='old', model='old', paused=True)
            jobs.create_job(prompt='script', schedule='every 1h', no_agent=True, script='/tmp/fixture.sh')
            before[location] = copy.deepcopy(jobs.load_jobs())
    settings = {'vps_hermes': {'config': {'manage_cron_job_models': True,
        'managed_overlay': {'model': {'provider': 'fixture-cloud', 'default': 'fixture-model'}}}}}
    assert configurator.sync_cron_models(home, settings) == 4
    stamps = {}
    for location in homes:
        with jobs.use_cron_store(location):
            expected = before[location]
            for record in expected:
                if not record['no_agent']:
                    record.update(provider='fixture-cloud', model='fixture-model', base_url=None)
            assert jobs.load_jobs() == expected
            stamps[location] = (location / 'cron/jobs.json').stat().st_mtime_ns
    assert configurator.sync_cron_models(home, settings) == 0
    for location in homes:
        assert (location / 'cron/jobs.json').stat().st_mtime_ns == stamps[location]
    settings['vps_hermes']['config']['managed_overlay']['cron'] = {'follow_chat_model': True}
    assert configurator.sync_cron_models(home, settings) == 4
    for location in homes:
        with jobs.use_cron_store(location):
            expected = before[location]
            for record in expected:
                if not record['no_agent']:
                    record.update(provider=None, model=None, base_url=None)
            assert jobs.load_jobs() == expected
    assert configurator.sync_cron_models(home, settings) == 0
    corrupt = home / 'cron/jobs.json'
    corrupt.write_text('{broken')
    try:
        configurator.sync_cron_models(home, settings)
    except RuntimeError:
        pass
    else:
        raise AssertionError('corrupt storage must fail the deployment')
    assert corrupt.read_text() == '{broken'
'''
        result = subprocess.run([sys.executable, '-c', script,
                                 str(ROOT / 'runtime/apply-config.py'), str(Path(UPSTREAM).resolve())],
                                text=True, capture_output=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shared_cron_model_reads_the_root_config_even_inside_a_profile(self):
        script = '''
import importlib.util, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[2])
spec = importlib.util.spec_from_file_location('shared', sys.argv[1])
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
with tempfile.TemporaryDirectory() as directory:
    home = Path(directory)
    profile = home / 'profiles/builder'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('cron: {model: profile-pin}')
    job = {'model': 'job-pin', 'provider': 'old', 'base_url': 'https://old.invalid'}
    for model in ('first-chat-choice', 'next-chat-choice'):
        route = {'model': model, 'provider': 'chat-provider', 'base_url': None}
        (home / 'config.yaml').write_text(json.dumps({
            'cron': {'follow_chat_model': True, 'shared_model': route}}))
        for location in (home, profile):
            os.environ['HERMES_HOME'] = str(location)
            assert shared.routed_job(job) == route
    assert job['model'] == 'job-pin'
    assert (profile / 'config.yaml').read_text() == 'cron: {model: profile-pin}'
'''
        result = subprocess.run([sys.executable, '-c', script,
                                 str(ROOT / 'runtime/cron_shared_model.py'), str(Path(UPSTREAM).resolve())],
                                text=True, capture_output=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_full_backup_walker_agrees_with_update_verifier(self):
        # Execute the pinned source's real walker and its constants without
        # importing the unrelated CLI/provider dependency graph.
        names = {"_QUICK_SNAPSHOTS_DIR", "_EXCLUDED_DIRS", "_EXCLUDED_ROOT_DIRS",
                 "_EXCLUDED_BACKUP_ROOT_DIRS", "_KEPT_CACHE_SUBDIRS", "_in_excluded_root_dir",
                 "_SQLITE_SIDECAR_SUFFIXES", "_EXCLUDED_SUFFIXES", "_EXCLUDED_NAMES",
                 "_EXCLUDED_PREFIXES", "_is_non_regular_path", "_should_exclude", "_iter_backup_files",
                 "LOCAL_RUNTIME_ROOT_DIRS", "RETIRED_GENERATION_DIR_SUFFIX"}
        namespace = {"Path": Path, "os": os, "stat": stat, "suppress": contextlib.suppress}
        for source in ("hermes_constants.py", "hermes_state_dbfile.py", "hermes_cli/backup.py"):
            tree = ast.parse(self.patched_source(source))
            selected = [node for node in tree.body if getattr(node, "name", "") in names
                        or isinstance(node, ast.Assign) and any(
                            isinstance(target, ast.Name) and target.id in names for target in node.targets)
                        or isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                        and node.target.id in names]
            exec("from __future__ import annotations\n" + ast.unparse(ast.Module(body=selected, type_ignores=[])),
                 namespace)
        spec = importlib.util.spec_from_file_location("backup_verifier", ROOT / "runtime/verify-update-state.py")
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / ".hermes"
            profile_names = ("builder", "cache", "models", "runtimes", "node", "browser_profiles",
                             "images", "audio", "videos", "documents", "screenshots", "citations")
            for prefix in ("", "skills/custom/", *(f"profiles/{name}/" for name in profile_names)):
                for relative in ("config.yaml", "SOUL.md", "gateway.lock", "personal.lock", "hermes-agent/SKILL.md", "node/bin/node",
                                 "models/model", "runtimes/tool", "cache/catalog.json", "cache/delegation/task.log",
                                 "cache/images/photo", "cache/audio/message", "cache/videos/clip",
                                 "cache/documents/file", "cache/screenshots/screen", "cache/citations/evidence",
                                 "browser-profile/Login Data", "browser-profiles/default/Cookies",
                                 "browser_profiles/default/Cookies", "skills/.archive/custom/SKILL.md"):
                    path = home / (prefix + relative)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("fixture")
            archive_path = home.parent / "backup.zip"
            native_files = list(namespace["_iter_backup_files"](home, archive_path))
            # Reproduce shutdown cleanup between inventory and archive writes.
            for lock in home.rglob("gateway.lock"):
                lock.unlink()
            with zipfile.ZipFile(archive_path, "w") as archive:
                for path, relative in native_files:
                    archive.write(path, relative.as_posix())
            snapshot = verifier.create_snapshot(home)
            self.assertEqual(set(snapshot["files"]), {str(relative) for _, relative in native_files})
            verifier.verify_backup(archive_path, snapshot)
            self.assertIn("personal.lock", snapshot["files"])
            # Only the known runtime lock is disposable, not arbitrary files.
            (home / "personal.lock").unlink()
            missing_file = home / "personal.lock"
            with zipfile.ZipFile(home.parent / "incomplete.zip", "w") as archive:
                with self.assertRaises(FileNotFoundError):
                    archive.write(missing_file, "personal.lock")

    def patched_source(self, path):
        source = (Path(UPSTREAM) / path).read_text()
        for target, marker, old, new in patches._PATCHES:
            if target == path:
                self.assertIn(old, source, marker)
                source = source.replace(old, new, 1)
        return source

    def tool_guardrails(self, **overrides):
        # Execute the pinned controller and result classifier. Only the unrelated
        # utils import is isolated; all inputs here are valid JSON.
        classification = ModuleType("agent.tool_result_classification")
        exec((Path(UPSTREAM) / "agent/tool_result_classification.py").read_text(),
             classification.__dict__)
        guardrails = ModuleType("hermes_guardrails_fixture")
        with mock.patch.dict(sys.modules, {
            "agent.tool_result_classification": classification,
            "utils": SimpleNamespace(safe_json_loads=json.loads),
            guardrails.__name__: guardrails,
        }):
            exec(self.patched_source("agent/tool_guardrails.py"), guardrails.__dict__)
        config = guardrails.ToolCallGuardrailConfig(**{
            "hard_stop_enabled": True, "exact_failure_warn_after": 1,
            "exact_failure_block_after": 4, "same_tool_failure_halt_after": 6,
            **overrides,
        })
        return guardrails, guardrails.ToolCallGuardrailController(config)

    def test_patch_failure_guidance_requires_fresh_read_and_current_revision(self):
        guardrails, controller = self.tool_guardrails()
        result = json.dumps({"error": "Could not find a match for old_string"})
        decision = controller.after_call("patch", {"path": "test.py"}, result)
        self.assertEqual(decision.action, "warn")
        self.assertFalse(decision.should_halt)
        for instruction in ("read_file", "already", "HEAD", "same", "diff"):
            self.assertIn(instruction, decision.message)
        self.assertIn(decision.message, guardrails.append_toolguard_guidance(result, decision))

    def test_write_refusal_guidance_preserves_read_before_write(self):
        _guardrails, controller = self.tool_guardrails()
        decision = controller.after_call("write_file", {"path": "test.py"},
                                         json.dumps({"error": "Refusing to overwrite unread file"}))
        self.assertEqual(decision.action, "warn")
        self.assertIn("read_file", decision.message)
        self.assertIn("overwrite", decision.message)
        self.assertIn("bypass", decision.message)

    def test_mutation_guidance_does_not_echo_replacement_contents(self):
        _guardrails, controller = self.tool_guardrails()
        args = {"path": "test.py", "old_string": "SYNTHETIC_SECRET_OLD",
                "new_string": "SYNTHETIC_SECRET_NEW"}
        decision = controller.after_call("patch", args, json.dumps({"error": "no match"}))
        self.assertNotIn(args["old_string"], decision.message)
        self.assertNotIn(args["new_string"], decision.message)

    def test_exact_failure_limit_survives_readonly_rechecks(self):
        for limit in (2, 4, 5):
            with self.subTest(limit=limit):
                _guardrails, controller = self.tool_guardrails(exact_failure_block_after=limit)
                args = {"path": "test.py", "old_string": "old", "new_string": "new"}
                for _ in range(limit):
                    self.assertEqual(controller.before_call("patch", args).action, "allow")
                    controller.after_call("patch", args, json.dumps({"error": "no match"}))
                controller.after_call("read_file", {"path": "test.py"}, json.dumps({"content": "new"}))
                decision = controller.before_call("patch", args)
                self.assertEqual(decision.code, "repeated_exact_failure_block")
                self.assertTrue(decision.should_halt)

    def test_corrected_payload_and_landed_patch_allow_progress(self):
        _guardrails, controller = self.tool_guardrails()
        args = {"path": "test.py", "old_string": "stale", "new_string": "fixed"}
        controller.after_call("patch", args, json.dumps({"error": "no match"}))
        corrected = {**args, "old_string": "current"}
        self.assertEqual(controller.before_call("patch", corrected).action, "allow")
        decision = controller.after_call("patch", corrected,
                                         json.dumps({"success": True, "diff": "landed"}))
        self.assertEqual(decision.action, "allow")
        self.assertEqual(controller.before_call("patch", args).action, "allow")

    def test_same_tool_failure_halt_remains_enabled(self):
        _guardrails, controller = self.tool_guardrails()
        for attempt in range(controller.config.same_tool_failure_halt_after):
            decision = controller.after_call("patch", {"old_string": str(attempt)},
                                             json.dumps({"error": "no match"}))
        self.assertEqual(decision.code, "same_tool_failure_halt")
        self.assertTrue(decision.should_halt)

    def test_disabled_warnings_do_not_force_recovery_guidance(self):
        _guardrails, controller = self.tool_guardrails(warnings_enabled=False)
        decision = controller.after_call("patch", {}, json.dumps({"error": "no match"}))
        self.assertEqual(decision.action, "allow")

    def test_landed_patch_with_diagnostics_is_not_reported_as_failed(self):
        _guardrails, controller = self.tool_guardrails()
        result = json.dumps({"success": True, "diff": "landed",
                             "lsp_diagnostics": "ERROR: synthetic diagnostic"})
        decision = controller.after_call("patch", {"path": "test.py"}, result)
        self.assertEqual(decision.action, "allow")
        self.assertFalse(controller._exact_failure_counts)

    def test_other_tools_keep_native_exact_failure_warning(self):
        _guardrails, controller = self.tool_guardrails()
        decision = controller.after_call("web_extract", {}, json.dumps({"error": "timeout"}))
        self.assertEqual(decision.code, "repeated_exact_failure_warning")
        self.assertNotIn("read_file", decision.message)

    def test_search_budget_warns_before_cap_without_losing_results(self):
        for cap, threshold in ((1, 1), (5, 4), (20, 16), (30, 24)):
            with self.subTest(cap=cap):
                module, _controller = self.tool_guardrails()
                _, controller = self.tool_guardrails(
                    loop_caps=module.LoopCapConfig(max_web_searches=cap))
                for count in range(1, cap + 1):
                    args = {"query": f"different query {count}"}
                    self.assertTrue(controller.before_call("web_search", args).allows_execution)
                    result = json.dumps({"results": [{"url": f"https://example.com/{count}"}]})
                    decision = controller.after_call("web_search", args, result)
                    self.assertFalse(decision.should_halt)
                    if count >= threshold:
                        self.assertEqual(decision.code, "loop_web_search_budget_warning")
                        self.assertIn(f"{cap - count} remaining", decision.message)
                        self.assertIn("web_extract", decision.message)
                        self.assertIn("partial", decision.message)
                        self.assertTrue(module.append_toolguard_guidance(result, decision).startswith(result))
                    else:
                        self.assertEqual(decision.action, "allow")
                decision = controller.before_call("web_search", {"query": "extra"})
                self.assertEqual(decision.code, "loop_web_search_cap")
                self.assertTrue(decision.should_halt)
                self.assertEqual(controller._turn_web_search_count, cap)
                controller.reset_for_turn()
                self.assertEqual(controller._turn_web_search_count, 0)

    def test_search_budget_respects_disabled_warnings_and_disabled_cap(self):
        module, _controller = self.tool_guardrails()
        for cap, warnings in ((0, True), (2, False)):
            with self.subTest(cap=cap, warnings=warnings):
                _, controller = self.tool_guardrails(
                    warnings_enabled=warnings,
                    loop_caps=module.LoopCapConfig(max_web_searches=cap))
                for count in range(2):
                    args = {"query": str(count)}
                    controller.before_call("web_search", args)
                    decision = controller.after_call("web_search", args, json.dumps({"results": [count]}))
                    self.assertEqual(decision.action, "allow")
                decision = controller.before_call("web_search", {"query": "extra"})
                self.assertEqual(decision.should_halt, bool(cap))

    def test_search_error_guidance_diagnoses_backend_without_repeating_queries(self):
        _module, controller = self.tool_guardrails()
        args = {"query": "SYNTHETIC_PRIVATE_QUERY"}
        controller.before_call("web_search", args)
        decision = controller.after_call("web_search", args, json.dumps({"error": "HTTP 429"}))
        self.assertEqual(decision.code, "repeated_exact_failure_warning")
        for instruction in ("rate", "credentials", "web_extract", "partial"):
            self.assertIn(instruction, decision.message)
        self.assertNotIn(args["query"], decision.message)
        for attempt in range(1, controller.config.same_tool_failure_halt_after):
            decision = controller.after_call("web_search", {"query": str(attempt)},
                                             json.dumps({"error": "HTTP 429"}))
        self.assertEqual(decision.code, "same_tool_failure_halt")
        self.assertTrue(decision.should_halt)

    def test_search_cap_report_distinguishes_budget_from_failed_retries(self):
        module, _controller = self.tool_guardrails()
        tree = ast.parse(self.patched_source("run_agent.py"))
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                      and node.name == "_toolguard_controlled_halt_response")
        namespace = {"ToolGuardrailDecision": module.ToolGuardrailDecision}
        exec(ast.unparse(ast.Module(body=[method], type_ignores=[])), namespace)
        report = namespace[method.name]
        for code, tool, message in (
            ("loop_web_search_cap", "web_search", "budget exhausted"),
            ("same_tool_failure_halt", "web_search", "Repeated search errors"),
            ("repeated_exact_failure_block", "patch", "Repeated patch errors"),
        ):
            with self.subTest(code=code):
                decision = module.ToolGuardrailDecision("block", code, message, tool, 20)
                text = report(SimpleNamespace(), decision)
                self.assertIn("incomplete", text.lower())
                self.assertNotIn("continue", text)
                self.assertNotIn("result above", text)
                if code == "loop_web_search_cap":
                    self.assertIn("20", text)
                    self.assertIn("successful", text)
                    self.assertNotIn("without making progress", text)
                else:
                    self.assertIn(message, text)

    def test_cron_rejects_guardrail_halt_even_when_native_result_says_completed(self):
        tree = ast.parse(self.patched_source("cron/scheduler.py"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_final_response_from_result")
        namespace = {"logger": mock.Mock()}
        exec(ast.unparse(ast.Module(body=[function], type_ignores=[])), namespace)
        finalize = namespace[function.name]
        agent = SimpleNamespace(_format_turn_completion_explanation=lambda *args, **kwargs: "")
        with mock.patch.dict(sys.modules, {
            "gateway.media_repair": SimpleNamespace(
                repair_explicit_computer_use_media_paths=lambda text, messages: text),
            "hermes_state_errors": SimpleNamespace(PERSISTENCE_ERROR_CAUSES=()),
        }):
            result = {"completed": True, "failed": False, "final_response": "Search budget exhausted",
                      "turn_exit_reason": "guardrail_halt"}
            with self.assertRaisesRegex(RuntimeError, "Search budget exhausted"):
                finalize(result, "fixture", "Monitor", agent)
            result["turn_exit_reason"] = "text_response(stop)"
            result["final_response"] = "Verified report"
            self.assertEqual(finalize(result, "fixture", "Monitor", agent), "Verified report")
            result.update(completed=False, turn_exit_reason="max_iterations_reached(10)")
            self.assertEqual(finalize(result, "fixture", "Monitor", agent), "Verified report")

    def test_attached_cron_skills_use_native_selection_and_global_denies(self):
        # Execute the pinned scheduler and model_tools selectors rather than a
        # second implementation of their allow/deny logic. Avoid importing the
        # unrelated providers, databases, and gateway dependency graph.
        toolsets = {}
        exec(self.patched_source("toolsets.py"), toolsets)
        namespace = {"os": os, "_LEGACY_TOOLSET_MAP": {},
                     "_is_delegated_child_context": lambda: False,
                     "_is_dispatcher_owned_worker": lambda: False}
        names = {"_with_cron_skill_tools", "_resolve_cron_enabled_toolsets",
                 "_resolve_cron_disabled_toolsets", "_merge_mcp_into_per_job_toolsets",
                 "_apply_toolset_selection", "_select_tool_names"}
        for path in ("cron/scheduler.py", "model_tools.py"):
            tree = ast.parse(self.patched_source(path))
            selected = [node for node in tree.body if getattr(node, "name", "") in names]
            exec("from __future__ import annotations\n" +
                 ast.unparse(ast.Module(body=selected, type_ignores=[])), namespace)
        static_tools = SimpleNamespace(
            get_toolset=lambda name: toolsets["TOOLSETS"].get(name),
            resolve_toolset=lambda name: toolsets["resolve_toolset"](name, include_registry=False),
            validate_toolset=lambda name: name in toolsets["TOOLSETS"],
            bundle_non_core_tools=toolsets["bundle_non_core_tools"])
        namespace.update(resolve_toolset=static_tools.resolve_toolset,
                         validate_toolset=static_tools.validate_toolset)
        modules = {"toolsets": static_tools,
                   "hermes_cli.tools_config": SimpleNamespace(
                       enabled_mcp_server_names=lambda cfg: set(),
                       _get_platform_tools=lambda cfg, platform: cfg["platform_toolsets"][platform]),
                   "agent.skill_utils": SimpleNamespace(parse_config_string_list=lambda value: value or [])}
        cfg = {"platform_toolsets": {"cron": ["web", "file"]}}
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, {}, clear=True):
            for enabled in (["terminal", "web", "file"], None):
                job = {"skills": ["competitor-news-monitor"]}
                if enabled is not None:
                    job["enabled_toolsets"] = enabled
                with self.subTest(enabled=enabled):
                    selected = namespace["_resolve_cron_enabled_toolsets"](job, cfg)
                    disabled = namespace["_resolve_cron_disabled_toolsets"](cfg)
                    effective = namespace["_select_tool_names"](selected, disabled, True)
                    self.assertLessEqual({"skill_view", "skills_list", "web_search", "web_extract"}, effective)
                    self.assertNotIn("skill_manage", effective)
                    denied_cfg = {**cfg, "agent": {"disabled_toolsets": ["skills", "terminal"]}}
                    denied = namespace["_resolve_cron_disabled_toolsets"](denied_cfg)
                    effective = namespace["_select_tool_names"](selected, denied, True)
                    self.assertNotIn("skill_view", effective)
                    self.assertNotIn("skills_list", effective)
                    self.assertNotIn("terminal", effective)
                    self.assertIn("web_search", effective)

    def test_split_gateway_dispatch_preserves_custom_commands(self):
        tree = ast.parse(self.patched_source("gateway/run_busy.py"))
        gateway = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                       and node.name == "GatewayBusySessionMixin")
        names = {"_COMMAND_HANDLER_ALIASES", "_PLAIN_COMMANDS", "_IDLE_COMMANDS",
                 "_command_handler_table", "_gateway_plain_command_handlers", "_gateway_idle_command_handlers"}
        gateway.body = [node for node in gateway.body if getattr(node, "name", "") in names
                        or isinstance(node, ast.Assign) and any(
                            isinstance(target, ast.Name) and target.id in names for target in node.targets)]
        model_tree = ast.parse(self.patched_source("gateway/slash_commands_model.py"))
        gateway.body.append(next(node for node in ast.walk(model_tree)
                                 if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_model_global_command"))
        namespace = {}
        exec("from __future__ import annotations\n" + ast.unparse(gateway), namespace)
        runner = namespace["GatewayBusySessionMixin"]()
        for name in (*runner._PLAIN_COMMANDS, *runner._IDLE_COMMANDS):
            method = runner._COMMAND_HANDLER_ALIASES.get(name, f"_handle_{name.replace('-', '_')}_command")
            if method != "_handle_model_global_command":
                setattr(runner, method, mock.AsyncMock(return_value="handled"))
        plain = runner._gateway_plain_command_handlers()
        self.assertIs(plain["gw-restart"], runner._handle_restart_command)
        self.assertIs(plain["doctor"], runner._handle_doctor_command)
        self.assertNotIn("model_global", plain)
        handler = runner._gateway_idle_command_handlers()["model_global"]
        for args, expected in (
            ("", "/model --global"),
            ("example --provider local", "/model example --provider local --global"),
            ("ollama-cloud/deepseek-ai/deepseek-v4.1-flash",
             "/model ollama-cloud/deepseek-ai/deepseek-v4.1-flash --global"),
        ):
            event = SimpleNamespace(text="/model_global", get_command_args=lambda: args)
            self.assertEqual(asyncio.run(handler(event)), "handled")
            self.assertEqual(event.text, expected)
            runner._handle_model_command.assert_awaited_with(event)

    def test_split_telegram_usage_preserves_priority_and_persistence(self):
        tree = ast.parse(self.patched_source("hermes_cli/commands_platforms.py"))
        functions = {"_telegram_usage_ranking_config", "_telegram_usage_state", "_write_telegram_usage_state",
                     "_telegram_command_usage_counts", "_telegram_command_usage_count", "record_telegram_command_usage",
                     "_prioritize_telegram_menu_candidates", "_sanitized_rank", "_sanitize_telegram_name",
                     "_telegram_command_menu_config"}
        constants = {"_TG_INVALID_CHARS", "_TG_MULTI_UNDERSCORE", "_CMD_NAME_LIMIT",
                     "_TELEGRAM_PRIORITY_TIERS", "_TELEGRAM_MENU_PRIORITY", "_telegram_usage_cache",
                     "_DEFAULT_TELEGRAM_MENU_MAX_COMMANDS", "_TELEGRAM_BOT_API_MAX_COMMANDS"}
        selected = [node for node in tree.body if getattr(node, "name", "") in functions
                    or isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id in constants for target in node.targets)
                    or isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in constants]
        config = {"platforms": {"telegram": {"extra": {"command_menu": {
            "usage_ranking": {"enabled": True, "refresh_every": 2},
            "priority": ["pinned"], "priority_mode": "prepend"}}}}}
        modules = {"hermes_cli": mock.Mock(), "hermes_cli.config": SimpleNamespace(read_raw_config=lambda: config)}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(sys.modules, modules):
            namespace = {"os": os, "re": __import__("re"), "Mapping": Mapping, "logger": logging.getLogger(__name__),
                         "_telegram_usage_state_path": lambda: str(Path(directory) / "usage.json")}
            code = ast.unparse(ast.Module(body=selected, type_ignores=[]))
            exec("from __future__ import annotations\n" + code, namespace)
            record = namespace["record_telegram_command_usage"]
            self.assertFalse(record("popular"))
            self.assertTrue(record("popular"))
            candidates = [(name, name, "core", name) for name in ("unranked", "popular", "help", "pinned")]
            ranked = namespace["_prioritize_telegram_menu_candidates"](candidates)
            self.assertEqual([item[0] for item in ranked], ["pinned", "help", "popular", "unranked"])
            self.assertEqual(namespace["_telegram_usage_state"](), ({"popular": 2}, 0))

    def test_pin_matches_source_and_installer(self):
        upstream = Path(UPSTREAM)
        settings = yaml.safe_load((ROOT / "config/vps-defaults.yml").read_text())
        pin = settings["vps_deploy"]["hermes_source"]
        commit = subprocess.check_output(
            ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True,
        ).strip()
        self.assertEqual(commit, pin["commit"])
        project = tomllib.loads((upstream / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["version"], pin["version"])
        self.assertEqual(
            hashlib.sha256((upstream / "scripts/install.sh").read_bytes()).hexdigest(),
            pin["installer_sha256"],
        )

    def test_all_gateway_patches_apply_compile_and_are_idempotent(self):
        paths = {path for path, _, _, _ in patches._PATCHES}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            for path in paths:
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(subprocess.check_output(
                    ['git', '-C', str(UPSTREAM), 'show', f'HEAD:{path}']))
            with mock.patch.object(patches, "HERMES_AGENT_DIR", destination):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    result = patches.main()
                self.assertEqual(result, 0, output.getvalue())
                for source_name, target_name in (("competitor_digest.py", "competitor_digest.py"),
                                                 ("cron_shared_model.py", "shared_model.py"),
                                                 ("ai_digest_run.py", "ai_digest_run.py")):
                    helper = destination / "cron" / target_name
                    self.assertEqual(helper.read_bytes(), (ROOT / "runtime" / source_name).read_bytes())
                    paths.add("cron/" + target_name)
                first = {path: (destination / path).read_text() for path in paths}
                for path, source in first.items():
                    if path.endswith('.py'):
                        compile(source, path, "exec")
                    # TS/TSX compilation and UI behavior are checked by
                    # test-dashboard-cron-ui.mjs against this same patch set.
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(patches.main(), 0)
                self.assertEqual(first, {path: (destination / path).read_text() for path in paths})

    def competitor_scheduler(self, outcome="delivered"):
        from hermes.runtime.test_competitor_digest import CompetitorDigestTests
        fixture = CompetitorDigestTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fence = mock.Mock(owner=None, cancel_event=None)
        fence.lost.return_value = False
        fence.transport_cancelled.return_value = False
        fence.side_effect_fence.side_effect = lambda: contextlib.nullcontext(True)
        ns = {"os": os, "logger": mock.Mock(), "_FireOwnership": mock.Mock(return_value=fence),
              "_get_hermes_home": lambda: fixture.home, "create_execution": lambda *a, **k: {"id": "exec"},
              "claim_dispatch": lambda *a: True, "mark_execution_running": lambda *a: True,
              "_RunDelivery": lambda **kw: SimpleNamespace(
                  delivery_attempted=False, delivery_error=None, should_deliver=False,
                  unresolved_origin=False, blocked_config=False, incident_acked=False,
                  failure_incident_id=None, side_effect_ownership_lost=False, **kw),
              "_FireClaimLostDuringSideEffect": type("ClaimLost", (Exception,), {}),
              "save_job_output": mock.Mock(return_value="saved.md"),
              "self_removal_delivery_allowed": lambda *a: False,
              "_is_interrupted": lambda *a: outcome == "interrupted",
              "_consume_interrupted_flag": lambda *a: False,
              "_compose_run_delivery": lambda job, **kw: (
                  kw["final_response"] if kw["success"] else "failure notice", False, False, False, None),
              "_is_cron_silence_response": lambda content: content == "[SILENT]",
              "SILENT_MARKER": "[SILENT]", "_normalize_deliver_value": lambda v: v,
              "_delivery_lane_value": lambda *a, **k: "origin",
              "_resolve_delivery_targets": lambda *a, **k: [] if outcome == "no_target" else ["telegram"],
              "_cron_failure_marker_error": lambda *a: None,
              "_finish_completed_run": mock.Mock(return_value=True),
              "_deliver_crash_failure": mock.Mock(return_value=(None, "delivered")),
              **{name: mock.Mock() for name in ("_teardown_cron_agent", "_record_fire_ownership_lost",
                                               "mark_job_run", "finish_execution", "_finish_interrupted_run")}}
        tree = ast.parse(self.patched_source("cron/scheduler.py"))
        selected = [node for node in tree.body if getattr(node, "name", "") in {
            "_run_one_job_body", "_save_compose_deliver"}]
        exec("from __future__ import annotations\n" + ast.unparse(ast.Module(body=selected, type_ignores=[])), ns)

        def run(job, **kw):
            prompt = kw["extra_prompt"] or ""
            self.assertIn("MANDATORY RUNTIME PUBLICATION CONTRACT", prompt)
            self.assertIsNotNone(kw["competitor_guard"])
            kw["defer_agent_teardown"].append("agent")
            path = Path(re.search(r"report artifact at (.+?\.json)", prompt)[1])
            guard = SimpleNamespace(run_id=path.stem,
                                    window_start=fixture.module.timestamp(re.search(r"window_start=([^,]+)", prompt)[1]),
                                    window_end=fixture.module.timestamp(re.search(r"window_end=(\S+?)\. JSON", prompt)[1]))
            record = fixture.record(guard, complete=True, events=outcome != "silent")
            if outcome == "invalid":
                record["coverage"].pop()
            path.write_text(json.dumps(record))
            fixture.watch_path.write_text(json.dumps(dict(fixture.watch, last_cutoff="2030-01-01")))
            if outcome == "cancel":
                raise KeyboardInterrupt()
            if outcome == "lost":
                fence.lost.return_value = True
            if outcome == "fenced":
                fence.side_effect_fence.side_effect = lambda: contextlib.nullcontext(False)
            return True, "Unvalidated output", "[SILENT]", None

        def deliver(job, content, **kw):
            if outcome == "queued":
                job["last_delivery_queued"] = True
            if outcome == "suppressed":
                job["_notification_all_targets_suppressed"] = True
            if outcome == "unverified":
                job["last_delivery_unverified"] = ["telegram"]
            if outcome == "cancel_after_send":
                fence.transport_cancelled.return_value = True
            return "delivery failed" if outcome == "failed" else None

        ns["run_job"] = mock.Mock(side_effect=run)
        ns["_deliver_result"] = mock.Mock(side_effect=deliver)
        secrets = SimpleNamespace(build_profile_secret_scope=mock.Mock(), set_secret_scope=mock.Mock(),
                                  reset_secret_scope=mock.Mock())
        terminal = SimpleNamespace(install_profile_terminal_scope=mock.Mock(), reset_terminal_scope=mock.Mock())
        from hermes.runtime.test_ai_digest_run import digest
        modules = mock.patch.dict(sys.modules, {"cron.competitor_digest": fixture.module,
                                  "cron.ai_digest_run": digest,
                                  "agent.secret_scope": secrets, "tools.terminal_scope": terminal})
        modules.start()
        self.addCleanup(modules.stop)
        return fixture, ns, secrets, terminal

    def competitor_agent_run(self, outcome="repaired", shared_route=None, research=False):
        fixture, _, _, _ = self.competitor_scheduler()
        if research:
            fixture.research_policy()
        guard = fixture.begin()
        fixture.save(guard, fixture.record(guard) if research else {"events": []})
        agent = mock.Mock()
        agent.iteration_budget.remaining = 0 if outcome == "no_iterations" else 20
        scope = mock.Mock(workdir=None, task_id="task-1")
        history = [{"role": "assistant", "content": "Evidence collected"}]
        cancellation = mock.Mock()
        responses = [{"completed": True, "final_response": "done", "messages": history},
                     {"completed": True, "final_response": "corrected", "messages": history}]
        if outcome == "no_history":
            responses[0].pop("messages")
        if outcome == "exhausted":
            responses[0]["completed"] = False
            responses[0]["error"] = "agent budget exhausted"
        if outcome == "iteration_summary":
            responses[0].update(completed=False, turn_exit_reason="max_iterations_reached(20)")

        def run_model(*args, **kwargs):
            if outcome == "provider_unreachable":
                raise RuntimeError("provider unavailable")
            agent.tool_progress_callback("tool.started", "web_search", None, {"query": "BetterMe mentions"})
            if kwargs.get("conversation_history") is not None:
                self.assertIs(args[0], agent)
                self.assertEqual(kwargs["conversation_history"], history)
                self.assertIs(args[6], cancellation)
                if research:
                    self.assertIn("ONE COVERAGE FOLLOW-UP", args[1])
                    self.assertIn('"remaining_search_calls": 3', args[1])
                if outcome == "timeout":
                    raise TimeoutError("repair timed out")
                if outcome == "repaired":
                    fixture.save(guard, fixture.record(guard, complete=True))
            return responses.pop(0)

        cancellation.is_set.return_value = outcome == "cancelled"
        namespace = {"logger": mock.Mock(), "_prepare_job_prompt": lambda *a: (None, "research"),
                     "_hermes_now": lambda: fixture.module.utc_now(),
                     "_CronRunScope": mock.Mock(return_value=scope),
                     "_load_cron_job_config": mock.Mock(return_value=SimpleNamespace(cfg={}, model="test-model")),
                     "_resolve_cron_agent_setup": mock.Mock(return_value=SimpleNamespace(
                         blocked=None, model="test-model", fallback_notice=None)),
                     "_construct_cron_agent": mock.Mock(return_value=agent),
                     "_run_agent_with_watchdog": mock.Mock(side_effect=run_model),
                     "_cron_failure_marker_error": lambda *a: None,
                     "_is_cron_silence_response": lambda s: s == "[SILENT]",
                     "_run_doc_header": lambda *a: "# Run\n", "_open_cron_session_db": lambda *a: None,
                     **{name: mock.Mock() for name in (
                         "_reload_dotenv_and_publish_delivery_target", "_FireAudit", "_teardown_cron_agent")}}
        tree = ast.parse(self.patched_source("cron/scheduler.py"))
        selected = [node for node in tree.body if getattr(node, "name", "") in {
            "run_job", "_final_response_from_result"}]
        exec("from __future__ import annotations\n" + ast.unparse(ast.Module(body=selected, type_ignores=[])), namespace)
        modules = {"run_agent": SimpleNamespace(AIAgent=mock.Mock()),
                   "gateway.media_repair": SimpleNamespace(
                       repair_explicit_computer_use_media_paths=lambda text, messages: text),
                   "cron.shared_model": SimpleNamespace(
                       routed_job=lambda job: {**job, **shared_route} if shared_route else job),
                   "cron.unreachable_retry": SimpleNamespace(is_model_unreachable_failure=lambda *a: True),
                   "cron.quota_hold": SimpleNamespace(hold_seconds_from_failure=lambda *a: 300),
                   "cron.scheduler_diagnostics": SimpleNamespace(format_run_error=lambda e: str(e)),
                   "cron.scheduler_detached_worker": SimpleNamespace(
                       defer_teardown_to_running_worker=lambda *a: False)}
        with mock.patch.dict(sys.modules, modules):
            success, output, response, error = namespace["run_job"](
                fixture.job, competitor_guard=guard, cancel_event=cancellation)
        namespace["_teardown_cron_agent"].assert_called_once_with(agent, fixture.job["id"])
        self.assertFalse(guard.state_path.exists())
        namespace["fixture_job"] = fixture.job
        namespace["research_record"] = guard.record
        return namespace, success, output, response, error

    def test_shared_cron_route_reaches_native_setup_without_losing_run_bookkeeping(self):
        route = {"model": "chat-choice", "provider": "chat-provider", "base_url": None}
        for outcome in ("repaired", "provider_unreachable"):
            with self.subTest(outcome=outcome):
                ns, success, _output, _response, _error = self.competitor_agent_run(outcome, route)
                original_job = ns["fixture_job"]
                model_job = ns["_load_cron_job_config"].call_args.args[0]
                self.assertEqual({key: model_job[key] for key in route}, route)
                self.assertIsNot(model_job, original_job)
                self.assertIs(ns["_resolve_cron_agent_setup"].call_args.args[0], model_job)
                self.assertIs(ns["_construct_cron_agent"].call_args.args[1], original_job)
                self.assertIs(ns["_run_agent_with_watchdog"].call_args.args[2], original_job)
                self.assertEqual(success, outcome == "repaired")
                if not success:
                    self.assertEqual(original_job["_quota_hold_seconds"], 300)
                    self.assertTrue(original_job["_model_unreachable"])

    def test_competitor_run_repairs_in_same_agent_with_history_and_watchdog(self):
        namespace, success, output, response, error = self.competitor_agent_run()
        self.assertTrue(success)
        self.assertIsNone(error)
        self.assertIn("Launched a team challenge", response)
        self.assertIn(response, output)
        self.assertEqual(namespace["_run_agent_with_watchdog"].call_count, 2)

    def test_competitor_followup_reuses_agent_history_and_observed_counts(self):
        ns, success, _output, response, error = self.competitor_agent_run(research=True)
        self.assertTrue(success, error)
        self.assertIn("Launched a team challenge", response)
        self.assertEqual(ns["_run_agent_with_watchdog"].call_count, 2)
        self.assertEqual(ns["research_record"]["tool_counts"]["web_search"], 2)

    def test_competitor_followup_is_bounded_by_cancellation_history_and_iterations(self):
        for outcome, calls, success in (("timeout", 2, False), ("cancelled", 1, False),
                                        ("no_history", 1, False), ("no_iterations", 1, True),
                                        ("exhausted", 1, False), ("iteration_summary", 1, True),
                                        ("invalid", 2, True)):
            with self.subTest(outcome=outcome):
                ns, actual, _output, response, _error = self.competitor_agent_run(outcome, research=True)
                self.assertEqual(actual, success)
                self.assertEqual(ns["_run_agent_with_watchdog"].call_count, calls)
                if success:
                    self.assertIn("Coverage: partial", response)
                    self.assertFalse(ns["research_record"]["coverage_complete"])

    def test_competitor_run_repair_failure_never_returns_success_or_retries_again(self):
        for outcome, calls, diagnostic in (("invalid", 2, "missing company coverage"),
                                           ("timeout", 2, "repair timed out"),
                                           ("cancelled", 1, "repair cancelled"),
                                           ("no_history", 1, "missing research history"),
                                           ("exhausted", 1, "budget exhausted")):
            with self.subTest(outcome=outcome):
                ns, success, _output, response, error = self.competitor_agent_run(outcome)
                self.assertFalse(success)
                self.assertEqual(response, "")
                self.assertIn(diagnostic, error)
                self.assertEqual(ns["_run_agent_with_watchdog"].call_count, calls)

    def test_cron_notification_reaches_telegram_important_mode(self):
        # Execute the pinned scheduler/adapter methods without sending a message.
        delivery = ast.parse((Path(UPSTREAM) / "cron/scheduler_delivery.py").read_text())
        selected = [n for n in delivery.body if getattr(n, "name", "") in {
            "_cron_delivery_notify_enabled", "_live_route_metadata"}]
        adapter = ast.parse((Path(UPSTREAM) / "plugins/platforms/telegram/adapter.py").read_text())
        selected += [n for cls in adapter.body if isinstance(cls, ast.ClassDef)
                     for n in cls.body if getattr(n, "name", "") == "_notification_kwargs"]
        ns = {"_is_channel_dm_topic": lambda *a: False}
        exec("from __future__ import annotations\n" + ast.unparse(ast.Module(body=selected, type_ignores=[])), ns)
        with mock.patch.dict(sys.modules, {
            "gateway.config": SimpleNamespace(Platform=SimpleNamespace(TELEGRAM="telegram")),
            "gateway.delivery": SimpleNamespace(_looks_like_int=lambda v: v.isdigit(),
                looks_like_telegram_private_chat_id=lambda v: v.isdigit()),
        }):
            for config, disabled in (({}, False), ({"cron": {"delivery": {"notify": False}}}, True)):
                for thread in (None, "123"):
                    target = SimpleNamespace(job={"id": "job"}, platform="telegram", thread_id=thread,
                        chat_id="42", runtime_adapter=None, loop=None, origin_target=False,
                        notify_delivery=ns["_cron_delivery_notify_enabled"](config))
                    _, metadata, media = ns["_live_route_metadata"](target)
                    for routed in (metadata, media):
                        kwargs = ns["_notification_kwargs"](SimpleNamespace(_notifications_mode="important"), routed)
                        self.assertEqual(kwargs.get("disable_notification", False), disabled)

    def test_competitor_patch_upgrades_existing_installation_and_is_idempotent(self):
        patch = next(p for p in patches._PATCHES if p[1] == "# Local Hermes: competitor bounded artifact repair")
        old_source = patch[3].replace(
            'completing cron report (one additional attempt)', 'correcting cron artifact (one attempt)')
        old_source = re.sub(
            r'            remaining = getattr\(getattr\(agent, "iteration_budget".*?remaining > 0\)\)\)',
            '            final_response = competitor_guard.finalize_with_repair(_repair_competitor_artifact)',
            old_source, flags=re.DOTALL)
        self.assertNotEqual(old_source, patch[3])
        state = {patch[1]: {"source": old_source, "digest": patches._patch_digest(old_source)}}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / patch[0]
            target.parent.mkdir()
            target.write_text(old_source)
            with mock.patch.object(patches, "HERMES_AGENT_DIR", Path(directory)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(patches._apply_one_patch(*patch, state), (1, None, True))
                self.assertEqual(target.read_text(), patch[3])
                self.assertEqual(patches._apply_one_patch(*patch, state), (0, None, False))

    def test_watchdog_forwards_history_only_when_repair_supplies_it(self):
        import concurrent.futures
        import contextvars
        import threading
        import time
        tree = ast.parse(self.patched_source("cron/scheduler.py"))
        selected = [node for node in tree.body if getattr(node, "name", "") == "_run_agent_with_watchdog"]
        ns = {"concurrent": __import__("concurrent"), "contextvars": contextvars,
              "threading": threading, "time": time, "_cron_inactivity_seconds": lambda: 0}
        exec("from __future__ import annotations\n" + ast.unparse(ast.Module(body=selected, type_ignores=[])), ns)
        for history in (None, [{"role": "assistant", "content": "sources"}]):
            with self.subTest(history=history):
                agent = mock.Mock()
                agent.run_conversation.return_value = {"completed": True}
                result = ns["_run_agent_with_watchdog"](
                    agent, "prompt", {}, "job", "name", "task", None, conversation_history=history)
                expected = {"conversation_history": history} if history is not None else {}
                agent.run_conversation.assert_called_once_with("prompt", task_id="task", **expected)
                self.assertTrue(result["completed"])

    def test_digest_scheduler_uses_current_artifacts_instead_of_model_summary(self):
        from hermes.runtime.test_ai_digest_run import DigestRunTests, SKILL_DIR
        digest_fixture = DigestRunTests()
        digest_fixture.setUp()
        self.addCleanup(digest_fixture.doCleanups)
        fixture, ns, _, _ = self.competitor_scheduler()
        fixture.job["skill"] = "ai_digest"
        fixture.job["skills"] = ["ai_digest"]

        def run(job, **kwargs):
            guard = kwargs["competitor_guard"]
            self.assertIn(f"--run-id {guard.run_id}", kwargs["extra_prompt"])
            guard.state.mkdir(parents=True, exist_ok=True)
            guard.raw_path.write_text(json.dumps({**digest_fixture.raw, "run_id": guard.run_id}))
            guard.draft_path.write_text(digest_fixture.draft)
            return True, "old misleading output", "Все карточки готовы", None

        ns["run_job"].side_effect = run
        with mock.patch.dict(os.environ, {"AI_DIGEST_SKILL_DIR": str(SKILL_DIR)}):
            self.assertTrue(ns["_run_one_job_body"](fixture.job))
        content = ns["_deliver_result"].call_args.args[1]
        self.assertTrue(content.startswith("NEWS_CARDS:"))
        self.assertNotIn("Все карточки готовы", content)
        self.assertIn(content, str(ns["save_job_output"].call_args))
        # Exercise the real delivery marker parser too: a generated marker alone
        # does not prove the scheduler can find and deliver its current artifact.
        from hermes.skills.ai_digest.scripts import feedback
        clean, artifact = feedback.extract_cards_marker(content, fixture.job, digest_fixture.home / 'ops/news')
        self.assertIsNotNone(artifact)
        self.assertTrue(artifact.is_file())
        self.assertNotIn('NEWS_CARDS:', clean)

    def test_execution_receipt_does_not_treat_unverified_transport_as_delivered(self):
        tree = ast.parse(self.patched_source("cron/scheduler.py"))
        selected = [node for node in tree.body if getattr(node, "name", "") in {
            "_finish_completed_run", "_classify_delivery_outcome"}]
        ns = {"self_removal_delivery_allowed": lambda *a: False,
              "mark_job_run": mock.Mock(return_value=True), "finish_execution": mock.Mock(),
              "_normalize_deliver_value": lambda v: v, "_delivery_lane_value": lambda *a, **k: "telegram"}
        exec("from __future__ import annotations\n" + ast.unparse(ast.Module(body=selected, type_ignores=[])), ns)
        run = SimpleNamespace(job={"id": "job", "last_delivery_unverified": ["telegram"]},
                              success=True, error=None, delivery_error=None, should_deliver=True,
                              blocked_config=False, unresolved_origin=False, incident_acked=False)
        self.assertTrue(ns["_finish_completed_run"](run, "owner", "exec-1"))
        ns["finish_execution"].assert_called_once_with(
            "exec-1", success=True, error=None, delivery_outcome="unverified")

    def manual_completion(self):
        tree = ast.parse(self.patched_source("tools/cronjob_tools.py"))
        names = {"_manual_run_completion", "_latest_job_output_excerpt", "_manual_run_delivery_note"}
        selected = [node for node in tree.body if getattr(node, "name", "") in names]
        namespace = {"time": SimpleNamespace(time=lambda: 100), "Path": Path,
                     "get_job": lambda *a: {"last_status": "ok", "next_run_at": "later"}}
        exec("from __future__ import annotations\n" + ast.unparse(ast.Module(body=selected, type_ignores=[])), namespace)
        return namespace

    def test_manual_completion_never_calls_failure_delivery_a_report(self):
        result = {"success": False, "error": "invalid artifact", "execution_id": "exec-1",
                  "execution_status": "failed", "delivery_outcome": "delivered"}
        ns = self.manual_completion()
        with mock.patch.dict(ns, {"_latest_job_output_excerpt": mock.Mock(return_value="OLD REPORT")}) as patched:
            completion = ns["_manual_run_completion"](result, "job-1", "Fitness", "telegram:123", 90)
            patched["_latest_job_output_excerpt"].assert_not_called()
        self.assertEqual(completion["status"], "error")
        self.assertIn("exec-1", completion["summary"])
        self.assertIn("failure notification", completion["summary"])
        self.assertNotIn("output was delivered", completion["summary"])
        self.assertNotIn("OLD REPORT", completion["summary"])

    def test_manual_completion_requires_confirmed_execution_delivery(self):
        ns = self.manual_completion()
        for status, outcome in ((None, None), ("completed", "queued"),
                                ("completed", "unverified"), ("completed", "suppressed")):
            with self.subTest(outcome=outcome):
                result = {"success": True, "execution_status": status, "delivery_outcome": outcome}
                completion = ns["_manual_run_completion"](result, "job-1", "Fitness", "telegram:123", 90)
                self.assertNotIn("report delivered", completion["summary"])
                self.assertNotIn("output was delivered", completion["summary"])

    def test_manual_completion_reads_exact_run_response_without_prompt_or_old_file(self):
        ns = self.manual_completion()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_dir = root / "job-1"
            job_dir.mkdir()
            current = job_dir / "current.md"
            current.write_text("# Cron Job: Fitness\n## Prompt\nINTERNAL INSTRUCTIONS\n## Response\nNEW REPORT")
            (job_dir / "z-later.md").write_text("UNRELATED REPORT")
            modules = {"cron.jobs": SimpleNamespace(get_cron_output_dir=lambda: root)}
            result = {"success": True, "execution_id": "exec-1", "execution_status": "completed",
                      "delivery_outcome": "delivered", "output_file": str(current)}
            with mock.patch.dict(sys.modules, modules):
                completion = ns["_manual_run_completion"](result, "job-1", "Fitness", "telegram:123", 90)
            self.assertIn("report delivered", completion["summary"])
            self.assertIn("NEW REPORT", completion["summary"])
            self.assertNotIn("INTERNAL INSTRUCTIONS", completion["summary"])
            self.assertNotIn("UNRELATED REPORT", completion["summary"])

    def test_competitor_scheduler_renders_artifact_before_delivery_and_checkpoints(self):
        fixture, ns, secrets, terminal = self.competitor_scheduler()
        self.assertTrue(ns["_run_one_job_body"](fixture.job))
        body = ns["_deliver_result"].call_args.args[1]
        self.assertIn("Launched a team challenge", body)
        self.assertTrue(fixture.job['_competitor_split_report'])
        self.assertNotIn("[SILENT]", body)
        self.assertIn(body, ns["save_job_output"].call_args.args[1])
        self.assertEqual(json.loads(fixture.watch_path.read_text())["last_run_status"], "ok")
        ns["_teardown_cron_agent"].assert_called_once()
        secrets.reset_secret_scope.assert_called_once()
        terminal.reset_terminal_scope.assert_called_once()
        fixture.begin()  # Lock was released after the scheduler finished.

    def test_competitor_scheduler_supplies_deployed_research_budget_to_agent(self):
        fixture, ns, _secrets, _terminal = self.competitor_scheduler()
        policy = fixture.research_policy()
        self.assertTrue(ns["_run_one_job_body"](fixture.job))
        prompt = ns["run_job"].call_args.kwargs["extra_prompt"]
        plan = json.loads(prompt.split("Plan JSON:\n", 1)[1])
        self.assertEqual(plan["max_page_attempts"], len(fixture.watch["companies"]) * policy["pages_per_company"])
        self.assertEqual(plan["company_order"], [c["name"] for c in fixture.watch["companies"]])
        self.assertIn("overrides earlier research budgets", prompt)

    def test_competitor_scheduler_failure_paths_never_checkpoint_or_leak_lock(self):
        for outcome in ("invalid", "failed", "queued", "no_target", "suppressed", "unverified",
                        "interrupted", "lost", "fenced", "cancel", "cancel_after_send"):
            with self.subTest(outcome=outcome):
                fixture, ns, secrets, terminal = self.competitor_scheduler(outcome)
                if outcome == "cancel":
                    with self.assertRaises(KeyboardInterrupt):
                        ns["_run_one_job_body"](fixture.job)
                else:
                    ns["_run_one_job_body"](fixture.job)
                self.assertEqual(json.loads(fixture.watch_path.read_text()), fixture.watch)
                if outcome in {"invalid", "lost", "fenced", "cancel"}:
                    ns["_deliver_result"].assert_not_called()
                ns["_teardown_cron_agent"].assert_called_once()
                secrets.reset_secret_scope.assert_called_once()
                terminal.reset_terminal_scope.assert_called_once()
                fixture.begin()

    def test_competitor_scheduler_zero_news_delivers_checklist_before_commit(self):
        fixture, ns, _secrets, _terminal = self.competitor_scheduler("silent")
        self.assertTrue(ns["_run_one_job_body"](fixture.job))
        ns["_deliver_result"].assert_called_once()
        self.assertTrue(ns["_deliver_result"].call_args.args[1].startswith("Cronjob Response: "))
        self.assertEqual(json.loads(fixture.watch_path.read_text())["last_run_status"], "ok")

    def test_competitor_delivery_sends_news_and_checklist_without_generic_wrapper(self):
        tree = ast.parse(self.patched_source('cron/scheduler_delivery.py'))
        selected = [n for n in tree.body if getattr(n, 'name', '') == '_deliver_result']
        live = mock.Mock(return_value=True)
        target = SimpleNamespace(live_adapter_ready=True, mirror_text='whole report')
        mirrors = []
        live.side_effect = lambda t, *args, **kwargs: mirrors.append(t.mirror_text) or True
        ns = dict(os=os, contextlib=contextlib, logger=logging.getLogger('test'), BOT_CHAT_PLATFORM='bot-chat',
                  _resolve_delivery_targets=lambda *a, **kw: [{'platform': 'telegram', 'chat_id': '123', 'thread_id': '615640'}],
                  _record_delivery_verification=mock.Mock(), _sched=SimpleNamespace(load_config=lambda: {}),
                  _cron_delivery_notify_enabled=lambda cfg: True, _cron_mirror_delivery_enabled=lambda *a: False,
                  _redact_cron_payload=lambda text, label: text.replace('sensitive-canary', '[redacted]'),
                  _prepare_target_delivery=mock.Mock(return_value=target), _deliver_via_live_adapter=live,
                  _deliver_standalone=mock.Mock())
        exec('from __future__ import annotations\n' + ast.unparse(ast.Module(body=selected, type_ignores=[])), ns)
        modules = {
            'gateway.config': SimpleNamespace(load_gateway_config=lambda: object()),
            'gateway.platforms.base': SimpleNamespace(BasePlatformAdapter=SimpleNamespace(
                extract_media=lambda text: ([], text), filter_media_delivery_paths=lambda paths: paths)),
            'gateway.media_policy': SimpleNamespace(apply_media_policy_env=lambda cfg: None),
            'gateway.warning_notifications': SimpleNamespace(warning_notifications_enabled=lambda *a: True),
        }
        job = {'id': 'job', 'name': 'Fitness', '_competitor_split_report': True}
        news = 'Noom — short description sensitive-canary\nhttps://example.com/news'
        details = 'Cronjob Response: Fitness\nNoom — 1 new. sensitive-canary'
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, {'_HERMES_CRON_EXTERNAL_WORKER': ''}):
            self.assertIsNone(ns['_deliver_result'](job, news + '\n\n' + details))
            sent = [call.args[1] for call in live.call_args_list]
            self.assertEqual(sent, [news.replace('sensitive-canary', '[redacted]'), details.replace('sensitive-canary', '[redacted]')])
            self.assertEqual(mirrors, sent)
            self.assertFalse(any('stop reminder' in text for text in sent))
            live.reset_mock()
            self.assertIsNone(ns['_deliver_result'](job, details))
            self.assertEqual(live.call_count, 1)
            live.reset_mock()
            self.assertIsNone(ns['_deliver_result']({'id': 'other', 'name': 'Other'}, 'ordinary report'))
            self.assertIn('To stop or manage', live.call_args.args[1])
            live.reset_mock()
            self.assertIsNone(ns['_deliver_result'](job, 'Failure notice', for_failure=True))
            self.assertIn('To stop or manage', live.call_args.args[1])
            live.reset_mock()
            live.side_effect = [True, False]
            ns['_deliver_standalone'].side_effect = lambda *args: args[-1].append('Checklist delivery failed')
            self.assertEqual(ns['_deliver_result'](job, news + '\n\n' + details), 'Checklist delivery failed')
            self.assertEqual(live.call_count, 2)

    def test_detached_delivery_preserves_unverified_receipt_for_publication_gate(self):
        tree = ast.parse(self.patched_source("cron/scheduler_delivery.py"))
        selected = [node for node in tree.body if getattr(node, "name", "") == "_deliver_result"]
        ns = {"os": os, "BOT_CHAT_PLATFORM": "bot-chat", "_record_delivery_verification": mock.Mock(),
              "_resolve_delivery_targets": lambda *a, **kw: [{"platform": "telegram"}]}
        exec("from __future__ import annotations\n" + ast.unparse(ast.Module(body=selected, type_ignores=[])), ns)
        job = {"id": "job", "execution_id": "exec", "_competitor_split_report": True}
        receipts = ["telegram:synthetic"]
        enqueue = mock.Mock(return_value=None)
        report = 'News\n\nCronjob Response: Fitness\nCompany checklist'
        with mock.patch.dict(os.environ, {"_HERMES_CRON_EXTERNAL_WORKER": "exec"}), mock.patch.dict(
            sys.modules, {"cron.delivery_queue": SimpleNamespace(
                enqueue_and_wait=enqueue, get_status=lambda *a: {"status": "delivered"}),
                "cron.jobs": SimpleNamespace(get_job=lambda *a: {"last_delivery_unverified": receipts})}
        ):
            self.assertIsNone(ns["_deliver_result"](job, report))
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[2], report)
        self.assertTrue(enqueue.call_args.args[1]['_competitor_split_report'])
        self.assertEqual(job.get("last_delivery_unverified"), receipts)


if __name__ == "__main__":
    unittest.main()

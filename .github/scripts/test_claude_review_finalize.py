"""Recover only a fully read, turn-limited SDK review, never a partial run."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock

import yaml

import claude_review_finalize as finalize


SESSION = 'd47dd0b4-8b56-41df-966d-3cae8ee6a80c'
REVIEW = {'summary': 'Reviewed all changes.', 'findings': [], 'thread_verdicts': []}


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.diff = self.root / '.ci-pr-review.diff'
        self.diff.write_text('diff --git a/app.py b/app.py\n+fixed\n')
        self.execution = self.root / 'execution.json'
        self.proof = self.root / 'proof.json'
        self.output = self.root / 'review.json'
        self.enterContext(mock.patch.object(Path, 'cwd', return_value=self.root))
        self.enterContext(mock.patch.object(finalize.context, '_changed_paths', return_value=set()))
        self.enterContext(mock.patch.dict(os.environ, {'BASE_SHA': 'a' * 40, 'HEAD_SHA': 'b' * 40}))

    def events(self):
        return [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [{
                'type': 'tool_use', 'id': 'read-1', 'name': 'Read',
                'input': {'file_path': str(self.diff)},
            }]}},
            {'type': 'user', 'message': {'content': [{
                'type': 'tool_result', 'tool_use_id': 'read-1',
                'content': '1→diff --git a/app.py b/app.py\n2→+fixed',
            }]}},
            {'type': 'result', 'subtype': 'error_max_turns', 'is_error': True,
             'session_id': SESSION},
        ]

    def prepare(self, events=None):
        self.execution.write_text(json.dumps(self.events() if events is None else events))
        return finalize.prepare(self.execution, self.proof)

    def completed(self):
        return [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'result', 'subtype': 'success', 'is_error': False,
             'session_id': SESSION, 'result': json.dumps(REVIEW)},
        ]

    def test_turn_limit_can_finish_same_session_without_repeating_tools(self):
        log = io.StringIO()
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            prepared = self.prepare()
            self.assertEqual(prepared['session_id'], SESSION)
            self.assertEqual(prepared['ready'], 'true')
            self.execution.write_text(json.dumps(self.completed()))
            finalize.extract(self.execution, self.proof, self.output)
        self.assertEqual(json.loads(self.output.read_text()), REVIEW)
        self.assertEqual(self.proof.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('diff --git', self.proof.read_text())
        self.assertNotIn('+fixed', log.getvalue())

    def test_completed_review_with_invalid_json_can_repair_output_in_same_session(self):
        events = self.events()
        events[-1].update(subtype='success', is_error=False, result='Completed review, but private invalid prose.')
        prepared = self.prepare(events)
        self.assertEqual(prepared['ready'], 'true')
        self.assertEqual(prepared['reason'], 'invalid_output_after_complete_diff_read')
        self.assertEqual(prepared['session_id'], SESSION)
        self.assertNotIn('private', json.dumps(prepared))
        self.execution.write_text(json.dumps(self.completed()))
        with contextlib.redirect_stdout(io.StringIO()):
            finalize.extract(self.execution, self.proof, self.output)
        self.assertEqual(json.loads(self.output.read_text()), REVIEW)

    def test_output_repair_requires_full_diff_evidence_even_after_sdk_success(self):
        events = self.events()
        events[-1].update(subtype='success', is_error=False, result='Not JSON')
        events[2]['message']['content'][0]['content'] = '1→diff --git a/app.py b/app.py'
        prepared = self.prepare(events)
        self.assertEqual(prepared['ready'], 'false')
        self.assertEqual(prepared['reason'], 'diff_not_fully_read')
        self.assertFalse(self.proof.exists())

    def test_valid_output_does_not_authorize_rewriting_findings(self):
        events = self.events()
        # This finding has valid schema but no changed anchor. Formatting repair
        # must not become a mechanism for deleting an invalid-anchor finding.
        document = {**REVIEW, 'findings': [{
            'severity': 'P2', 'path': 'unchanged.py', 'side': 'RIGHT', 'line': 999,
            'title': 'Fixture finding', 'impact': 'Fixture impact', 'fix': 'Fixture fix',
        }]}
        events[-1].update(subtype='success', is_error=False, result=json.dumps(document))
        self.assertEqual(self.prepare(events)['ready'], 'false')
        self.assertFalse(self.proof.exists())

    def test_parseable_json_with_invalid_schema_is_eligible_only_for_format_repair(self):
        events = self.events()
        events[-1].update(subtype='success', is_error=False, result='{"summary":"Reviewed"}')
        self.assertEqual(self.prepare(events)['reason'], 'invalid_output_after_complete_diff_read')

    def test_missing_final_text_can_be_completed_only_with_read_proof(self):
        events = self.events()
        events[-1].update(subtype='success', is_error=False)
        self.assertEqual(self.prepare(events)['reason'], 'invalid_output_after_complete_diff_read')

    def test_paginated_reads_preserve_blank_lines_and_indentation(self):
        self.diff.write_text('first\n\n\tindented\nlast\n')
        events = self.events()
        events[2]['message']['content'][0]['content'] = [
            {'type': 'image', 'data': 'ignored'},
            {'type': 'text', 'text': '  1→first\n  2→'},
        ]
        page_call, page_result = copy.deepcopy(events[1:3])
        page_call['message']['content'][0].update(id='page-2')
        page_call['message']['content'][0]['input'].update(offset=3, limit=2)
        page_result['message']['content'][0].update(tool_use_id='page-2', content='3\t\tindented\n4:last')
        events[3:3] = [page_call, page_result]
        self.assertEqual(self.prepare(events)['ready'], 'true')

    def test_unmatched_reused_and_pre_session_reads_do_not_count(self):
        for case in ('wrong_id', 'reused_id', 'before_init', 'no_numbered_text', 'invalid_content'):
            events = self.events()
            if case == 'wrong_id':
                events[2]['message']['content'][0]['tool_use_id'] = 'other'
            elif case == 'reused_id':
                unrelated = copy.deepcopy(events[1])
                unrelated['message']['content'][0]['name'] = 'Grep'
                events.insert(2, unrelated)
            elif case == 'before_init':
                events = [events[1], events[2], events[0], events[3]]
            elif case == 'no_numbered_text':
                events[2]['message']['content'][0]['content'] = 'file too large'
            else:
                events[2]['message']['content'][0]['content'] = None
            with self.subTest(case=case):
                self.assertEqual(self.prepare(events)['ready'], 'false')
                self.assertFalse(self.proof.exists())

    def test_invalid_or_oversized_diff_cannot_create_proof(self):
        for text in ('', '\x80'):
            self.diff.write_bytes(text.encode('latin1'))
            with self.subTest(text=text):
                self.assertEqual(self.prepare()['ready'], 'false')
        self.diff.write_bytes(b'x' * 5000)
        with mock.patch.object(finalize.context, 'MAX_CLAUDE_EXECUTION_FILE_BYTES', 4096):
            self.assertEqual(self.prepare()['ready'], 'false')
        self.assertFalse(self.proof.exists())

    def test_missing_partial_failed_or_unrelated_read_cannot_be_finalized(self):
        for case in ('no_read', 'partial', 'failed', 'other_file', 'subagent', 'after_result', 'wrong_text'):
            events = self.events()
            if case == 'no_read':
                events = [events[0], events[-1]]
            elif case == 'partial':
                events[2]['message']['content'][0]['content'] = '1→diff --git a/app.py b/app.py'
            elif case == 'failed':
                events[2]['message']['content'][0]['is_error'] = True
            elif case == 'other_file':
                events[1]['message']['content'][0]['input']['file_path'] = str(self.root / 'other')
            elif case == 'subagent':
                events[1]['parent_tool_use_id'] = 'delegate'
            elif case == 'after_result':
                events = [events[0], events[-1], *events[1:3]]
            else:
                events[2]['message']['content'][0]['content'] = '1→different diff\n2→+fixed'
            with self.subTest(case=case):
                self.assertEqual(self.prepare(events)['ready'], 'false')
                self.assertFalse(self.proof.exists())

    def test_other_sdk_failures_and_missing_logs_never_resume(self):
        for subtype in ('error_during_execution', 'success', 'error_max_budget_usd'):
            events = self.events()
            events[-1]['subtype'] = subtype
            with self.subTest(subtype=subtype):
                self.assertEqual(self.prepare(events)['ready'], 'false')
        self.execution.unlink()
        self.assertEqual(finalize.prepare(self.execution, self.proof)['ready'], 'false')

    def test_missing_log_is_distinguished_from_incomplete_diff(self):
        self.assertEqual(finalize.prepare(self.execution, self.proof)['reason'], 'execution_unavailable')
        events = self.events()
        events[2]['message']['content'][0]['content'] = '1→diff --git a/app.py b/app.py'
        self.assertEqual(self.prepare(events)['reason'], 'diff_not_fully_read')

    def test_partial_read_reports_exact_coverage_without_exposing_content(self):
        events = self.events()
        events[2]['message']['content'][0]['content'] = '1→diff --git a/app.py b/app.py'
        call, result = copy.deepcopy(events[1:3])
        call['message']['content'][0]['id'] = 'failed-read'
        result['message']['content'][0].update(tool_use_id='failed-read', is_error=True, content='private error')
        events[3:3] = [call, result]
        prepared = self.prepare(events)
        self.assertEqual(prepared['reason'], 'diff_not_fully_read')
        self.assertEqual(json.loads(prepared['read_coverage']), {
            'matched_lines': 1, 'total_lines': 2, 'read_calls': 2, 'failed_reads': 1,
        })
        self.assertNotIn('private', json.dumps(prepared))
        self.assertFalse(self.proof.exists())

    def test_read_plan_covers_every_line_in_bounded_pages_without_source_text(self):
        lines = ['private source ' + 'Ж' * 100 for _ in range(650)]
        self.diff.write_text('\n'.join(lines) + '\n')
        plan = finalize.read_plan()
        self.assertEqual(plan['total_lines'], len(lines))
        self.assertEqual(plan['diff_bytes'], self.diff.stat().st_size)
        self.assertNotIn('private', json.dumps(plan))
        visited = []
        for page in plan['pages']:
            first = page['offset'] - 1
            indexes = range(first, first + page['limit'])
            visited.extend(indexes)
            self.assertLessEqual(page['limit'], finalize.MAX_READ_PAGE_LINES)
            self.assertLessEqual(sum(len(f'{i + 1}→{lines[i]}\n'.encode()) for i in indexes), finalize.MAX_READ_PAGE_BYTES)
        self.assertEqual(visited, list(range(len(lines))))

    def test_read_plan_honors_line_limit_and_isolates_oversized_lines_without_dropping_them(self):
        self.diff.write_text('\n' * 650)
        plan = finalize.read_plan()
        self.assertGreater(len(plan['pages']), 1)
        self.assertTrue(all(page['limit'] <= finalize.MAX_READ_PAGE_LINES for page in plan['pages']))
        self.diff.write_text('before\n' + 'x' * finalize.MAX_READ_PAGE_BYTES + '\nafter\n')
        self.assertEqual(finalize.read_plan()['pages'], [
            {'offset': 1, 'limit': 1}, {'offset': 2, 'limit': 1}, {'offset': 3, 'limit': 1},
        ])

    def test_proof_write_failure_has_a_safe_distinct_reason(self):
        with mock.patch.object(finalize.os, 'replace', side_effect=OSError('private path')):
            result = self.prepare()
        self.assertEqual(result, {'ready': 'false', 'reason': 'proof_unavailable'})
        self.assertFalse(self.proof.exists())

    def test_invalid_or_mixed_session_ids_never_enter_action_arguments(self):
        for value in ('', '" --model other', 'd47dd0b4-8b56-41df-966d-3cae8ee6a80d'):
            events = self.events()
            events[-1]['session_id'] = value
            with self.subTest(value=value):
                self.assertEqual(self.prepare(events)['ready'], 'false')
        events = self.events()
        events[0]['session_id'] = events[-1]['session_id'] = '" --model other'
        self.assertEqual(self.prepare(events)['ready'], 'false')

    def test_resumed_errors_other_sessions_and_tool_calls_are_rejected(self):
        self.prepare()
        for case in ('error', 'no_result', 'other_session', 'tool', 'subagent', 'invalid_json', 'missing_text'):
            events = self.completed()
            if case == 'error':
                events[-1].update(is_error=True, subtype='error_max_turns')
            elif case == 'no_result':
                events.pop()
            elif case == 'other_session':
                for event in events:
                    event['session_id'] = 'd47dd0b4-8b56-41df-966d-3cae8ee6a80d'
            elif case in ('tool', 'subagent'):
                event = self.events()[1]
                if case == 'subagent':
                    event['parent_tool_use_id'] = 'parent'
                events.insert(1, event)
            elif case == 'invalid_json':
                events[-1]['result'] = 'not a review'
            else:
                del events[-1]['result']
            self.execution.write_text(json.dumps(events))
            with self.subTest(case=case), self.assertRaises(RuntimeError):
                finalize.extract(self.execution, self.proof, self.output)
            self.assertFalse(self.output.exists())

    def test_proof_is_bound_to_both_revisions_and_exact_diff(self):
        self.prepare()
        proof = json.loads(self.proof.read_text())
        self.execution.write_text(json.dumps(self.completed()))
        for key in ('base_sha', 'head_sha', 'diff_sha256'):
            changed = copy.deepcopy(proof)
            changed[key] = 'c' * 64
            self.proof.write_text(json.dumps(changed))
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                finalize.extract(self.execution, self.proof, self.output)
        self.assertFalse(self.output.exists())

    def test_invalid_proof_never_accepts_a_successful_response(self):
        self.execution.write_text(json.dumps(self.completed()))
        for proof in (' ' * 4097, '{}', '[]'):
            self.proof.write_text(proof)
            with self.subTest(length=len(proof)), self.assertRaises(RuntimeError):
                finalize.extract(self.execution, self.proof, self.output)
        self.assertFalse(self.output.exists())

    def run_cli(self, *arguments):
        with (
            mock.patch.object(sys, 'argv', [finalize.__file__, *arguments]),
            # Other suites load this module under the same name via importlib.
            mock.patch.dict(sys.modules, {'pr_review_context': finalize.context}),
        ):
            runpy.run_path(finalize.__file__, run_name='__main__')

    def test_cli_passes_only_validated_session_id_then_extracts_result(self):
        outputs = self.root / 'github-output'
        self.execution.write_text(json.dumps(self.events()))
        common = ('--execution-file', str(self.execution), '--proof-file', str(self.proof))
        with mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(outputs)}), contextlib.redirect_stdout(io.StringIO()):
            self.run_cli('prepare', *common)
            self.execution.write_text(json.dumps(self.completed()))
            self.run_cli('extract', *common, '--output', str(self.output))
        self.assertIn('ready=true\n', outputs.read_text())
        self.assertIn('session_id=' + SESSION + '\n', outputs.read_text())
        self.assertEqual(json.loads(self.output.read_text()), REVIEW)

    def test_plan_cli_outputs_only_numeric_ranges_and_needs_no_execution_log(self):
        output = self.root / 'github-output'
        with mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}), contextlib.redirect_stdout(io.StringIO()):
            self.run_cli('plan')
        key, value = output.read_text().strip().split('=', 1)
        self.assertEqual(key, 'read_plan')
        self.assertEqual(json.loads(value)['pages'], [{'offset': 1, 'limit': 2}])
        self.assertFalse(self.proof.exists())

    def test_finalization_cli_still_requires_execution_and_proof_paths(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for arguments in (('prepare',), ('extract', '--execution-file', str(self.execution))):
                with self.subTest(command=arguments[0]), self.assertRaises(SystemExit) as error:
                    self.run_cli(*arguments)
                self.assertEqual(error.exception.code, 2)

    def test_workflow_provides_same_read_plan_to_each_full_review_attempt(self):
        workflow = yaml.safe_load((Path(__file__).parents[1] / 'workflows/pr-ai-review.yml').read_text())
        steps = {step.get('id'): step for step in workflow['jobs']['claude-code-plugin-review']['steps']}
        self.assertIn('claude_review_finalize.py plan', steps['claude_diff']['run'])
        for route in ('primary', 'primary_retry', 'fallback', 'fallback_retry'):
            self.assertIn('steps.claude_diff.outputs.read_plan', steps['claude_review_' + route]['with']['prompt'])
        report = steps['report_claude_review_unavailable']
        for route in ('PRIMARY', 'FALLBACK'):
            self.assertIn('outputs.read_coverage', report['env'][route + '_DIFF_READ'])
            self.assertIn('$' + route + '_DIFF_READ', report['run'])

    def test_cli_rejections_do_not_expose_execution_or_exception_text(self):
        self.execution.write_text('secret response')
        common = ('--execution-file', str(self.execution), '--proof-file', str(self.proof))
        outputs = self.root / 'github-output'
        log = io.StringIO()
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            with mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(outputs)}):
                self.run_cli('prepare', *common)
            with self.assertRaises(SystemExit) as missing:
                self.run_cli('extract', *common)
            self.assertEqual(missing.exception.code, 2)
            with self.assertRaises(SystemExit) as invalid:
                self.run_cli('extract', *common, '--output', str(self.output))
        self.assertIn('no review result was produced', str(invalid.exception))
        self.assertNotIn('secret response', log.getvalue())
        self.assertNotIn('session_id=', outputs.read_text())
        self.assertIn('ready=false\n', outputs.read_text())
        self.assertFalse(self.output.exists())

    def test_workflow_finalization_is_bounded_and_cannot_publish_without_validation(self):
        workflow = yaml.safe_load((Path(__file__).parents[1] / 'workflows/pr-ai-review.yml').read_text())
        job = workflow['jobs']['claude-code-plugin-review']
        steps = {step.get('id'): step for step in job['steps']}
        for route in ('primary', 'fallback'):
            with self.subTest(route=route):
                step = steps['claude_review_' + route + '_finalize']
                self.assertIn("!cancelled()", step['if'])
                self.assertIn("outputs.ready == 'true'", step['if'])
                self.assertIn('--resume', step['with']['claude_args'])
                self.assertIn('--tools=', step['with']['claude_args'])
                self.assertIn('--disallowedTools "mcp__*"', step['with']['claude_args'])
                self.assertIn('--max-turns 1', step['with']['claude_args'])
                self.assertLessEqual(step['timeout-minutes'], 2)
                self.assertLessEqual(steps['claude_review_' + route]['timeout-minutes'], 5)
                self.assertIn('extract_claude_review_' + route + '_finalize.outcome', steps['publish_claude_review']['if'])
        self.assertIn('extract_claude_review_primary_finalize.outcome', steps['claude_review_fallback']['if'])
        cleanup = steps['clear_claude_fallback_execution']
        self.assertIn(cleanup['if'], steps['claude_review_fallback']['if'])
        self.assertIn("steps.clear_claude_fallback_execution.outcome == 'success'", steps['claude_review_fallback']['if'])
        self.assertIn('"${RUNNER_TEMP}/claude-execution-output.json"', cleanup['run'])
        ids = list(steps)
        self.assertLess(ids.index('claude_review_primary_retry'), ids.index('clear_claude_fallback_execution'))
        self.assertLess(ids.index('clear_claude_fallback_execution'), ids.index('claude_review_fallback'))
        self.assertNotIn('API_TIMEOUT_MS', workflow.get('env', {}))
        self.assertLessEqual(int(job['env']['API_TIMEOUT_MS']), 90000)


if __name__ == '__main__':
    unittest.main()

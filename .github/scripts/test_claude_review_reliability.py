"""Regressions from PR #75: malformed output, turn exhaustion and repeated scope."""
import copy
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

import claude_review_full as full
import claude_review_finalize as finalize
import pr_review_context as context


REVIEW = {'summary': 'Checked changes.', 'findings': [], 'thread_verdicts': []}
SESSION = 'd47dd0b4-8b56-41df-966d-3cae8ee6a80c'


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.enterContext(mock.patch.object(Path, 'cwd', return_value=self.root))
        self.enterContext(mock.patch.dict(os.environ, {
            'BASE_SHA': 'a' * 40, 'HEAD_SHA': 'b' * 40,
            'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'RUNNER_TEMP': str(self.root),
        }))
        self.diff = self.root / '.ci-pr-review.diff'
        self.diff.write_text('first\nsecond\n')
        self.execution = self.root / 'execution.json'
        self.proof = self.root / 'proof.json'
        self.output = self.root / 'claude-review-result.json'

    def events(self, result=None):
        return [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [{
                'type': 'tool_use', 'id': 'read', 'name': 'Read',
                'input': {'file_path': str(self.diff)},
            }]}},
            {'type': 'user', 'message': {'content': [{
                'type': 'tool_result', 'tool_use_id': 'read',
                'content': '1→first\n2→second',
            }]}},
            {'type': 'result', 'session_id': SESSION, 'subtype': 'success',
             'is_error': False, **(result or {'structured_output': REVIEW})},
        ]

    def test_structured_sdk_result_is_extracted_with_full_read_evidence(self):
        self.execution.write_text(json.dumps(self.events()))
        with mock.patch.object(context, '_changed_paths', return_value=set()):
            full.extract(self.execution, self.output)
        self.assertEqual(json.loads(self.output.read_text()), REVIEW)

    def test_plain_json_result_is_extracted_with_full_read_evidence(self):
        self.execution.write_text(json.dumps(self.events({'result': json.dumps(REVIEW)})))
        with mock.patch.object(context, '_changed_paths', return_value=set()):
            full.extract(self.execution, self.output)
        self.assertEqual(json.loads(self.output.read_text()), REVIEW)

    def test_full_review_preparation_keeps_large_diff_without_chunking(self):
        diff = 'diff --git a/app.py b/app.py\n' + '+changed line\n' * 4000
        with mock.patch.object(full, 'exact_diff', return_value=diff):
            prepared = full.prepare()
        self.assertEqual(self.diff.read_text(), diff)
        self.assertEqual(set(prepared), {'read_plan'})
        self.assertEqual(prepared['read_plan']['total_lines'], 4001)
        self.assertEqual(sum(page['limit'] for page in prepared['read_plan']['pages']), 4001)

    def test_full_review_rejects_empty_and_oversized_diffs(self):
        for diff in ('', 'x' * (context.MAX_CLAUDE_EXECUTION_FILE_BYTES + 1)):
            with self.subTest(diff_size=len(diff)), mock.patch.object(full, 'exact_diff', return_value=diff):
                with self.assertRaisesRegex(RuntimeError, 'invalid_diff_size'):
                    full.prepare()

    def test_full_review_rejects_a_failed_terminal_sdk_result(self):
        events = self.events()
        events[-1].update(subtype='error_api_error', is_error=True)
        self.execution.write_text(json.dumps(events))
        with self.assertRaisesRegex(RuntimeError, 'review_execution_failed'):
            full.extract(self.execution, self.output)
        self.assertFalse(self.output.exists())

    def test_full_review_cli_writes_prepare_outputs_and_requires_extract_paths(self):
        outputs = self.root / 'github-output'
        payload = {'read_plan': {'total_lines': 2}}
        with mock.patch.object(full, 'prepare', return_value=payload), \
                mock.patch.object(sys, 'argv', [full.__file__, 'prepare']), \
                mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(outputs)}):
            full.main()
        self.assertEqual(dict(line.split('=', 1) for line in outputs.read_text().splitlines()), {
            key: json.dumps(value, separators=(',', ':')) for key, value in payload.items()
        })
        with mock.patch.object(sys, 'argv', [full.__file__, 'extract']), \
                self.assertRaises(SystemExit) as failure:
            full.main()
        self.assertEqual(failure.exception.code, 2)

    def test_full_review_rejects_partial_read_and_accepts_complete_result(self):
        events = self.events()
        events[2]['message']['content'][0]['content'] = '1→first'
        self.execution.write_text(json.dumps(events))
        with self.assertRaises(finalize.IncompleteDiffRead):
            full.extract(self.execution, self.output)
        self.assertFalse(self.output.exists())
        self.execution.write_text(json.dumps(self.events()))
        with mock.patch.object(full.context, '_changed_paths', return_value=set()):
            full.extract(self.execution, self.output)
        self.assertEqual(json.loads(self.output.read_text()), REVIEW)

    def test_full_review_cli_prepares_the_exact_git_revision_and_validates_all_pages(self):
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()
        git('init', '-q')
        git('config', 'user.email', 'fixture@example.com')
        git('config', 'user.name', 'Fixture')
        source = self.root / 'app.py'
        source.write_text('old\n')
        git('add', 'app.py')
        git('commit', '-qm', 'base')
        base = git('rev-parse', 'HEAD')
        source.write_text('changed\n' * 4000)
        git('add', 'app.py')
        git('commit', '-qm', 'head')
        head = git('rev-parse', 'HEAD')
        outputs = self.root / 'github-output'
        env = {**os.environ, 'BASE_SHA': base, 'HEAD_SHA': head, 'GITHUB_OUTPUT': str(outputs)}
        command = [sys.executable, full.__file__]
        prepared = subprocess.run([*command, 'prepare'], cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        expected = subprocess.check_output(['git', '-C', str(self.root), 'diff', '--unified=3', base, head, '--', '.'], text=True)
        self.assertEqual(self.diff.read_text(), expected)
        plan = json.loads(outputs.read_text().splitlines()[0].split('=', 1)[1])
        self.assertGreater(len(plan['pages']), 1)
        events = self.events()
        events[2]['message']['content'][0]['content'] = '\n'.join(
            f'{index}→{line}' for index, line in enumerate(expected.splitlines(), 1))
        self.execution.write_text(json.dumps(events))
        result = subprocess.run([*command, 'extract', '--execution-file', str(self.execution),
                                 '--output', str(self.output)], cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.output.read_text()), REVIEW)
        events[-1]['is_error'] = True
        self.execution.write_text(json.dumps(events))
        self.output.unlink()
        result = subprocess.run([*command, 'extract', '--execution-file', str(self.execution),
                                 '--output', str(self.output)], cwd=self.root, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_invalid_structured_output_cannot_hide_behind_valid_ordinary_text(self):
        event = self.events({'structured_output': {'summary': 'private-response'},
                             'result': json.dumps(REVIEW)})[-1]
        with self.assertRaises(RuntimeError):
            context._result_event_text(event)

    def test_malformed_enum_values_fail_with_safe_validation_codes(self):
        finding = {'severity': 'P2', 'path': 'app.py', 'side': 'RIGHT', 'line': 1,
                   'title': 'Bug', 'impact': 'Impact', 'fix': 'Fix'}
        verdict = {'thread_id': 'thread', 'verdict': 'needs_human', 'reason': 'Uncertain'}
        for field in ('severity', 'side', 'verdict'):
            document = copy.deepcopy(REVIEW)
            if field == 'verdict':
                document['thread_verdicts'] = [{**verdict, field: ['private-response']}]
            else:
                document['findings'] = [{**finding, field: ['private-response']}]
            with self.subTest(field=field):
                with self.assertRaises(RuntimeError) as failure:
                    context._validate_claude_result_contract(document)
                self.assertIn(context.review_failure_code(failure.exception),
                              ('invalid_finding_value', 'invalid_thread_verdict_value'))
        self.assertEqual(context.review_failure_code(RuntimeError('private-response')), 'validation_unavailable')

    def test_output_retry_exhaustion_can_resume_only_after_complete_read(self):
        events = self.events({'subtype': 'error_max_structured_output_retries', 'is_error': True})
        self.execution.write_text(json.dumps(events))
        prepared = finalize.prepare(self.execution, self.proof)
        self.assertEqual(prepared['ready'], 'true')
        events[2]['message']['content'][0]['content'] = '1→first'
        self.execution.write_text(json.dumps(events))
        self.assertEqual(finalize.prepare(self.execution, self.proof)['ready'], 'false')

    def test_complete_read_reports_numeric_coverage_for_recovery(self):
        self.execution.write_text(json.dumps(self.events({'subtype': 'error_max_turns', 'is_error': True})))
        prepared = finalize.prepare(self.execution, self.proof)
        self.assertEqual(json.loads(prepared['read_coverage']),
                         {'matched_lines': 2, 'total_lines': 2, 'read_calls': 1, 'failed_reads': 0})

    def test_finalization_accepts_only_the_sdk_output_sink(self):
        events = self.events({'subtype': 'error_max_turns', 'is_error': True})
        self.execution.write_text(json.dumps(events))
        self.assertEqual(finalize.prepare(self.execution, self.proof)['ready'], 'true')
        completed = [events[0],
            {'type': 'assistant', 'message': {'content': [{
                'type': 'tool_use', 'id': 'output', 'name': 'StructuredOutput', 'input': REVIEW,
            }]}},
            self.events()[-1]]
        self.execution.write_text(json.dumps(completed))
        with mock.patch.object(context, '_changed_paths', return_value=set()):
            finalize.extract(self.execution, self.proof)
        self.assertEqual(json.loads(self.output.read_text()), REVIEW)
        self.output.unlink()
        for name, value in (('Read', REVIEW), ('mcp__StructuredOutput', REVIEW),
                            ('StructuredOutput', {**REVIEW, 'summary': 'different'})):
            invalid = copy.deepcopy(completed)
            invalid[1]['message']['content'][0].update(name=name, input=value)
            self.execution.write_text(json.dumps(invalid))
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                finalize.extract(self.execution, self.proof)
            self.assertFalse(self.output.exists())

    def test_resumed_json_content_cannot_select_the_output_path(self):
        victim = self.root / 'victim.txt'
        victim.write_text('original')
        events = self.events({'subtype': 'error_max_turns', 'is_error': True})
        self.execution.write_text(json.dumps(events))
        self.assertEqual(finalize.prepare(self.execution, self.proof)['ready'], 'true')
        document = {**REVIEW, 'summary': str(victim)}
        completed = [events[0], {**self.events()[-1], 'structured_output': None,
                                'result': json.dumps(document)}]
        self.execution.write_text(json.dumps(completed))
        with mock.patch.object(finalize.context, '_changed_paths', return_value=set()):
            finalize.extract(self.execution, self.proof)
        self.assertEqual(json.loads(self.output.read_text()), document)
        self.assertEqual(victim.read_text(), 'original')

    def test_full_review_content_cannot_select_the_output_path(self):
        victim = self.root / 'victim.txt'
        victim.write_text('original')
        document = {**REVIEW, 'summary': str(victim)}
        self.execution.write_text(json.dumps(self.events({'structured_output': document})))
        with mock.patch.object(full.context, '_changed_paths', return_value=set()):
            full.extract(self.execution, self.output)
        self.assertEqual(json.loads(self.output.read_text()), document)
        self.assertEqual(victim.read_text(), 'original')

    def test_cli_diagnostic_names_invalid_schema_without_echoing_response(self):
        events = self.events({'structured_output': {'summary': 'private-response'}})
        self.execution.write_text(json.dumps(events))
        with mock.patch.object(sys, 'argv', [full.__file__, 'extract', '--execution-file', str(self.execution),
                                           '--output', str(self.output)]), self.assertRaises(SystemExit) as failure:
            runpy.run_path(full.__file__, run_name='__main__')
        self.assertIn('invalid_review_keys', str(failure.exception))
        self.assertNotIn('private-response', str(failure.exception))
        self.assertFalse(self.output.exists())

    def test_schema_enforces_the_existing_review_contract(self):
        schema = context.claude_review_schema()
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['required']), set(REVIEW))
        finding = schema['properties']['findings']['items']
        self.assertFalse(finding['additionalProperties'])
        self.assertEqual(finding['properties']['severity']['enum'], ['P1', 'P2'])
        self.assertEqual(finding['properties']['line'], {'type': 'integer', 'minimum': 1})
        self.assertEqual(schema['properties']['findings']['maxItems'], 5)
        self.assertEqual(schema['properties']['thread_verdicts']['maxItems'], 200)

    def test_compact_diff_retains_all_changed_lines_and_uses_exact_revisions(self):
        response = mock.Mock(stdout='fixture diff')
        with mock.patch.object(full.subprocess, 'run', return_value=response) as command:
            self.assertEqual(full.exact_diff(), 'fixture diff')
        arguments = command.call_args.args[0]
        self.assertIn('--unified=3', arguments)
        self.assertIn('a' * 40, arguments)
        self.assertIn('b' * 40, arguments)


class WorkflowReliabilityTests(unittest.TestCase):
    def test_claude_review_runs_one_complete_diff_in_one_job(self):
        workflow = yaml.safe_load((Path(__file__).parents[1] / 'workflows/pr-ai-review.yml').read_text())
        jobs = workflow['jobs']
        self.assertNotIn('claude-review-plan', jobs)
        self.assertNotIn('claude-review-chunks', jobs)
        job = jobs['claude-code-plugin-review']
        self.assertEqual(job['needs'], 'direct-api-review')
        self.assertNotIn('strategy', job)
        steps = {step.get('id'): step for step in job['steps']}
        self.assertIn('claude_review_full.py prepare', steps['claude_diff']['run'])
        self.assertIn('claude_review_full.py extract', steps['extract_claude_review_primary']['run'])
        self.assertIn('steps.publish_claude_review.outcome', steps['require_claude_completion']['env']['PUBLICATION_OUTCOME'])

    def test_sdk_output_and_context_are_bounded_for_every_route(self):
        workflow = yaml.safe_load((Path(__file__).parents[1] / 'workflows/pr-ai-review.yml').read_text())
        job = workflow['jobs']['claude-code-plugin-review']
        steps = {step.get('id'): step for step in job['steps']}
        self.assertNotIn('strategy', job)
        for route in ('primary', 'primary_retry', 'fallback', 'fallback_retry', 'primary_finalize', 'fallback_finalize'):
            step = steps['claude_review_' + route]
            self.assertNotIn('--json-schema', step['with']['claude_args'])
            self.assertNotIn('StructuredOutput', step['with']['claude_args'])
            if not route.endswith('finalize'):
                self.assertIn('steps.review_threads.outputs.context', step['with']['prompt'])
                self.assertIn('steps.sonar_context.outputs.context', step['with']['prompt'])
                self.assertIn('exactly one JSON object', step['with']['prompt'])
            else:
                self.assertIn('Return exactly', step['with']['prompt'])
                self.assertIn('--max-turns 3', step['with']['claude_args'])
                self.assertIn('--tools=', step['with']['claude_args'])
        for source in ('review_threads', 'sonar_context'):
            self.assertIn(source, steps)
            self.assertIn("steps.claude_models.outputs.primary_ready == 'true'", steps[source]['if'])
            self.assertIn("steps.claude_models.outputs.fallback_ready == 'true'", steps[source]['if'])
        self.assertEqual(job['permissions'], {'contents': 'read', 'issues': 'write', 'pull-requests': 'write'})
        self.assertIn('always()', job['if'])
        self.assertIn('!cancelled()', job['if'])


if __name__ == '__main__':
    unittest.main()

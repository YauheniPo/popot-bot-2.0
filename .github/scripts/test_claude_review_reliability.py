"""Regressions from PR #75: malformed output, turn exhaustion and repeated scope."""
import copy
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock

import yaml

import claude_review_chunks as chunks
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
            'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
        }))
        self.diff = self.root / '.ci-pr-review.diff'
        self.diff.write_text('first\nsecond\n')
        self.execution = self.root / 'execution.json'
        self.proof = self.root / 'proof.json'
        self.output = self.root / 'result.json'

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
            chunks.extract(self.execution, self.output)
        self.assertEqual(json.loads(self.output.read_text()), REVIEW)

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
            finalize.extract(self.execution, self.proof, self.output)
        self.assertEqual(json.loads(self.output.read_text()), REVIEW)
        self.output.unlink()
        for name, value in (('Read', REVIEW), ('mcp__StructuredOutput', REVIEW),
                            ('StructuredOutput', {**REVIEW, 'summary': 'different'})):
            invalid = copy.deepcopy(completed)
            invalid[1]['message']['content'][0].update(name=name, input=value)
            self.execution.write_text(json.dumps(invalid))
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                finalize.extract(self.execution, self.proof, self.output)
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
            finalize.extract(self.execution, self.proof, self.output)
        self.assertEqual(json.loads(self.output.read_text()), document)
        self.assertEqual(victim.read_text(), 'original')

    def test_chunk_report_content_cannot_select_the_output_path(self):
        victim = self.root / 'victim.txt'
        victim.write_text('original')
        document = {**REVIEW, 'summary': str(victim)}
        result = self.root / 'review.json'
        result.write_text(json.dumps(document))
        unit = {'index': 1, 'start': 1, 'end': 2, 'sha256': chunks._digest(self.diff.read_text())}
        with mock.patch.object(chunks, 'plan', return_value={'binding': {}, 'chunks': [unit]}), \
                mock.patch.object(sys, 'argv', [chunks.__file__, 'record', '--index', '1',
                    '--sha256', unit['sha256'], '--result', str(result), '--output', str(self.output)]), \
                mock.patch.dict(os.environ, {'CLAUDE_REVIEW_PROVIDER': 'fixture', 'CLAUDE_REVIEW_MODEL': 'fixture/model'}):
            chunks.main()
        self.assertEqual(json.loads(self.output.read_text())['result'], document)
        self.assertEqual(victim.read_text(), 'original')

    def test_cli_diagnostic_names_invalid_schema_without_echoing_response(self):
        events = self.events({'structured_output': {'summary': 'private-response'}})
        self.execution.write_text(json.dumps(events))
        with mock.patch.object(sys, 'argv', [chunks.__file__, 'extract', '--execution-file', str(self.execution),
                                           '--output', str(self.output)]), self.assertRaises(SystemExit) as failure:
            runpy.run_path(chunks.__file__, run_name='__main__')
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

    def test_scoped_context_includes_continuations_and_rename_sides(self):
        diff = ('diff --git a/old.py b/new.py\nsimilarity index 80%\n'
                'rename from old.py\nrename to new.py\n--- a/old.py\n+++ b/new.py\n'
                '@@ -1,2 +1,2 @@\n-old\n+new\n same\n'
                'diff --git a/other.py b/other.py\n--- a/other.py\n+++ b/other.py\n'
                '@@ -1 +1 @@\n-before\n+after\n')
        self.assertEqual(chunks.scope_paths(diff, 9, 10), {'old.py', 'new.py'})
        self.assertEqual(chunks.scope_paths(diff, 10, 16), {'old.py', 'new.py', 'other.py'})
        threads = [{'thread_id': 'a', 'path': 'new.py'}, {'thread_id': 'b', 'path': 'other.py'}]
        sonar = {'source': 'SonarCloud', 'issues': [{'path': 'new.py'}, {'path': 'other.py'}]}
        scoped = chunks.scoped_context({'new.py'}, json.dumps(threads), json.dumps(sonar))
        self.assertEqual(scoped['thread_context'], threads[:1])
        self.assertEqual(scoped['sonar_context']['issues'], sonar['issues'][:1])

    def test_scoped_context_handles_quoted_paths_and_binary_or_mode_only_changes(self):
        diff = ('diff --git "a/path\\tname.py" "b/path\\tname.py"\n'
                'old mode 100644\nnew mode 100755\n'
                'diff --git a/image.png b/image.png\nBinary files a/image.png and b/image.png differ\n')
        self.assertEqual(chunks.scope_paths(diff, 2, 5), {'path\tname.py', 'image.png'})
        quoted = 'diff --git "a/space b/path.py" "b/space b/path.py"\nold mode 100644\nnew mode 100755\n'
        self.assertEqual(chunks.scope_paths(quoted, 2, 3), {'space b/path.py'})
        unicode_path = 'diff --git "a/Ã©\\t.py" "b/Ã©\\t.py"\nold mode 100644\nnew mode 100755\n'
        self.assertEqual(chunks.scope_paths(unicode_path, 1, 3), {'Ã©\t.py'})
        octal_path = 'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\nold mode 100644\nnew mode 100755\n'
        self.assertEqual(chunks.scope_paths(octal_path, 1, 3), {'café.py'})

    def test_unchanged_threads_are_assigned_once_and_context_errors_fail_closed(self):
        threads = [{'path': 'app.py'}, {'path': 'unchanged.py'}, {'path': 'other.py'}]
        first = chunks.scoped_context({'app.py'}, json.dumps(threads), '{"issues":[]}', {'app.py', 'other.py'})
        later = chunks.scoped_context({'other.py'}, json.dumps(threads), '{"issues":[]}')
        self.assertEqual(first['thread_context'], threads[:2])
        self.assertEqual(later['thread_context'], threads[2:])
        for thread_json, sonar_json in (('{}', '{}'), ('[]', '[]'), ('[]', '{"issues":{}}')):
            with self.subTest(threads=thread_json), self.assertRaises(RuntimeError):
                chunks.scoped_context({'app.py'}, thread_json, sonar_json)

    def test_compact_diff_retains_all_changed_lines_and_uses_exact_revisions(self):
        response = mock.Mock(stdout='fixture diff')
        with mock.patch.object(chunks.subprocess, 'run', return_value=response) as command:
            self.assertEqual(chunks.exact_diff(), 'fixture diff')
        arguments = command.call_args.args[0]
        self.assertIn('--unified=3', arguments)
        self.assertIn('a' * 40, arguments)
        self.assertIn('b' * 40, arguments)


class WorkflowReliabilityTests(unittest.TestCase):
    def test_sdk_output_and_context_are_bounded_for_every_route(self):
        workflow = yaml.safe_load((Path(__file__).parents[1] / 'workflows/pr-ai-review.yml').read_text())
        job = workflow['jobs']['claude-review-chunks']
        steps = {step.get('id'): step for step in job['steps']}
        self.assertIn('matrix.index', job['name'])
        self.assertTrue(job['strategy']['fail-fast'])
        self.assertEqual(job['strategy']['max-parallel'], 1)
        for route in ('primary', 'primary_retry', 'fallback', 'fallback_retry', 'primary_finalize', 'fallback_finalize'):
            step = steps['claude_review_' + route]
            if route.endswith('finalize'):
                # PR #75: resume returned terminal success without the optional
                # structured_output field. The action rejects --json-schema
                # before our proof/JSON validator can inspect that result.
                self.assertNotIn('--json-schema', step['with']['claude_args'])
                self.assertIn('Return exactly', step['with']['prompt'])
                self.assertIn('--max-turns 3', step['with']['claude_args'])
                self.assertIn('--tools=', step['with']['claude_args'])
            else:
                self.assertIn('--json-schema', step['with']['claude_args'])
                self.assertIn('steps.claude_diff.outputs.json_schema', step['with']['claude_args'])
                self.assertIn('steps.claude_diff.outputs.thread_context', step['with']['prompt'])
                self.assertIn('steps.claude_diff.outputs.sonar_context', step['with']['prompt'])
                self.assertIn('StructuredOutput', step['with']['prompt'])
        for source in ('review_threads', 'sonar_context'):
            plan_steps = {step.get('id'): step for step in workflow['jobs']['claude-review-plan']['steps']}
            self.assertIn(source, plan_steps)
            self.assertNotIn(source, steps)
            self.assertIn("steps.claude_models.outputs.primary_ready == 'true'", plan_steps[source]['if'])
            self.assertIn("steps.claude_models.outputs.fallback_ready == 'true'", plan_steps[source]['if'])
        self.assertEqual(job['permissions'], {'contents': 'read'})
        aggregate = workflow['jobs']['claude-code-plugin-review']
        self.assertIn('always()', aggregate['if'])
        self.assertIn('!cancelled()', aggregate['if'])
        self.assertIn('claude-review-chunks', aggregate['needs'])


if __name__ == '__main__':
    unittest.main()

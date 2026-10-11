"""Bounded SDK work and fail-closed collection of revision-bound results."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

import claude_review_chunks as chunks


REVIEW = {'summary': 'Checked changes.', 'findings': [], 'thread_verdicts': []}
SESSION = 'd47dd0b4-8b56-41df-966d-3cae8ee6a80c'


class ChunkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.enterContext(mock.patch.object(Path, 'cwd', return_value=self.root))
        self.enterContext(mock.patch.dict(os.environ, {
            'BASE_SHA': 'a' * 40, 'HEAD_SHA': 'b' * 40,
            'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
            'CLAUDE_REVIEW_CHUNK_MAX_BYTES': '100',
            'CLAUDE_REVIEW_CHUNK_MAX_LINES': '3', 'CLAUDE_REVIEW_MAX_CHUNKS': '100',
        }))
        self.diff = 'diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-old\n+new\n same\n'
        self.enterContext(mock.patch.object(chunks, 'exact_diff', return_value=self.diff))

    def reports(self):
        plan = chunks.plan()
        reports = []
        for unit in plan['chunks']:
            reports.append({
                'binding': plan['binding'], 'chunk': unit, 'status': 'success',
                'result': copy.deepcopy(REVIEW), 'provider': 'fixture', 'model': 'fixture/model',
            })
        return reports

    def test_plan_bounds_every_chunk_and_covers_exact_diff_once(self):
        plan = chunks.plan()
        lines = self.diff.splitlines(keepends=True)
        reconstructed = []
        for unit in plan['chunks']:
            part = ''.join(lines[unit['start'] - 1:unit['end']])
            reconstructed.append(part)
            self.assertLessEqual(len(part.encode()), 100)
            self.assertLessEqual(len(part.splitlines()), 3)
        self.assertEqual(''.join(reconstructed), self.diff)
        self.assertEqual(plan['chunks'][0]['start'], 1)
        self.assertEqual(plan['chunks'][-1]['end'], len(lines))

    def test_split_inside_hunk_supplies_file_and_line_context(self):
        plan = chunks.plan()
        selected = chunks.select(plan['chunks'][-1]['index'], plan['chunks'][-1]['sha256'])
        self.assertEqual((self.root / '.ci-pr-review.diff').read_text(), ' same\n')
        self.assertEqual(selected['context']['file_header'], 'diff --git a/app.py b/app.py')
        self.assertEqual(selected['context']['next_left'], 2)
        self.assertEqual(selected['context']['next_right'], 2)

    def test_oversized_line_or_chunk_limit_fails_without_truncation(self):
        for diff, env in (('+' + 'я' * 60 + '\n', {}), (self.diff, {'CLAUDE_REVIEW_MAX_CHUNKS': '1'})):
            with self.subTest(diff=diff), mock.patch.object(chunks, 'exact_diff', return_value=diff), mock.patch.dict(os.environ, env):
                with self.assertRaises(RuntimeError):
                    chunks.plan()
        self.assertFalse((self.root / '.ci-pr-review.diff').exists())

    def test_selection_rejects_changed_hash_or_invalid_index(self):
        for index, digest in ((1, '0' * 64), (0, '0' * 64), (99, '0' * 64)):
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                chunks.select(index, digest)

    def test_complete_reports_merge_but_missing_duplicate_or_failed_unit_rejects(self):
        reports = self.reports()
        result = chunks.aggregate(reports)
        self.assertEqual(result['findings'], [])
        for invalid in (reports[:-1], reports + [reports[0]], [{**r, 'status': 'unavailable'} for r in reports]):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                chunks.aggregate(invalid)

    def test_results_bind_both_revisions_diff_run_and_attempt(self):
        for field in ('base_sha', 'head_sha', 'diff_sha256', 'run_id', 'run_attempt'):
            reports = self.reports()
            reports[0]['binding'] = {**reports[0]['binding'], field: 'stale'}
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                chunks.aggregate(reports)
        reports = self.reports()
        reports[0]['chunk']['end'] += 1
        with self.assertRaises(RuntimeError):
            chunks.aggregate(reports)

    def test_merge_deduplicates_findings_and_does_not_close_conflicting_threads(self):
        reports = self.reports()
        finding = {'severity': 'P2', 'path': 'app.py', 'side': 'RIGHT', 'line': 1,
                   'title': 'Bug', 'impact': 'Breaks requests.', 'fix': 'Add guard.'}
        reports[0]['result']['findings'] = [finding]
        reports[1]['result']['findings'] = [{**finding, 'severity': 'P1'}]
        for report, verdict in zip(reports[:2], ('fixed', 'confirmed')):
            report['result']['thread_verdicts'] = [{'thread_id': 'thread1', 'verdict': verdict, 'reason': 'app.py:1 checked.'}]
        result = chunks.aggregate(reports)
        self.assertEqual(len(result['findings']), 1)
        self.assertEqual(result['findings'][0]['severity'], 'P1')
        self.assertEqual(result['thread_verdicts'][0]['verdict'], 'needs_human')

    def test_successful_json_with_partial_read_cannot_be_extracted(self):
        diff = self.root / '.ci-pr-review.diff'
        diff.write_text(self.diff)
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'r', 'name': 'Read', 'input': {'file_path': str(diff)}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'r', 'content': '1→' + self.diff.splitlines()[0]}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': SESSION, 'result': json.dumps(REVIEW)},
        ]
        execution = self.root / 'execution.json'
        output = self.root / 'result.json'
        execution.write_text(json.dumps(events))
        log = io.StringIO()
        with contextlib.redirect_stdout(log), self.assertRaises(chunks.finalize.IncompleteDiffRead):
            chunks.extract(execution, output)
        self.assertFalse(output.exists())
        self.assertIn('"matched_lines": 1', log.getvalue())
        self.assertIn('"total_lines": 7', log.getvalue())
        self.assertNotIn('diff --git', log.getvalue())
        events[2]['message']['content'][0]['content'] = '\n'.join(f'{i}→{line}' for i, line in enumerate(self.diff.splitlines(), 1))
        execution.write_text(json.dumps(events))
        with mock.patch.object(chunks.context, '_changed_paths', return_value=set()):
            chunks.extract(execution, output)
        self.assertEqual(json.loads(output.read_text()), REVIEW)

    def test_real_git_diff_is_pinned_to_base_and_head(self):
        def git(*args):
            return subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()
        git('init', '-q')
        (self.root / 'app.py').write_text('old\n')
        git('add', 'app.py')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'base')
        base = git('rev-parse', 'HEAD')
        (self.root / 'app.py').write_text('new\n')
        git('add', 'app.py')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'head')
        head = git('rev-parse', 'HEAD')
        (self.root / 'app.py').write_text('uncommitted\n')
        with mock.patch.object(chunks, 'exact_diff', wraps=self.original_diff), mock.patch.dict(os.environ, {'BASE_SHA': base, 'HEAD_SHA': head}):
            diff = chunks.exact_diff()
        self.assertIn('+new\n', diff)
        self.assertNotIn('uncommitted', diff)

    original_diff = staticmethod(chunks.exact_diff)

    def test_cli_records_and_collects_complete_results_without_sdk_traces_or_keys(self):
        reports = self.root / 'reports'
        reports.mkdir()
        output = self.root / 'merged.json'
        github_output = self.root / 'github-output'
        result = self.root / 'review.json'
        result.write_text(json.dumps(REVIEW))
        def invoke(*args):
            with mock.patch.object(sys, 'argv', ['chunks', *args]), contextlib.redirect_stdout(io.StringIO()):
                chunks.main()
        env = {'GITHUB_OUTPUT': str(github_output), 'CLAUDE_REVIEW_PROVIDER': 'nous',
               'CLAUDE_REVIEW_MODEL': 'backup', 'CLAUDE_REVIEW_PRIMARY_MODEL': 'primary',
               'CLAUDE_REVIEW_PRIMARY_PROVIDER': 'openrouter', 'CLAUDE_REVIEW_FALLBACK_PROVIDER': 'nous',
               'CLAUDE_REVIEW_FALLBACK_MODEL': 'backup', 'NOUS_API_KEY': 'private-key',
               'CLAUDE_REVIEW_ENDPOINT': 'https://private.example?key=private-key',
               'CLAUDE_REVIEW_PRIMARY_OUTCOME': 'failure',
               'CLAUDE_REVIEW_FALLBACK_OUTCOME': 'success', 'CLAUDE_REVIEW_FALLBACK_VALIDATION': 'success'}
        with mock.patch.dict(os.environ, env):
            invoke('plan')
            matrix = json.loads(github_output.read_text().split('=', 1)[1])
            for unit in matrix['include']:
                folder = reports / str(unit['index'])
                folder.mkdir()
                common = ('--index', str(unit['index']), '--sha256', unit['sha256'])
                invoke('select', *common)
                invoke('record', *common, '--result', str(result), '--output', str(folder / 'chunk-result.json'))
            invoke('aggregate', '--reports', str(reports), '--output', str(output))
        self.assertEqual(json.loads(output.read_text()), REVIEW)
        metadata = ''.join(file.read_text() for file in reports.rglob('*.json')) + github_output.read_text()
        for forbidden in ('private-key', 'private.example', 'tool_result', 'session_id'):
            self.assertNotIn(forbidden, metadata)
        self.assertIn('chunk_executions=', github_output.read_text())
        self.assertIn('models=backup\n', github_output.read_text())

    def test_invalid_contract_cannot_enter_aggregate(self):
        reports = self.reports()
        reports[0]['result']['findings'] = [{'title': 'Incomplete'}]
        with self.assertRaises(RuntimeError):
            chunks.aggregate(reports)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2]
        self.jobs = yaml.safe_load((root / '.github/workflows/pr-ai-review.yml').read_text())['jobs']
        self.action = yaml.safe_load((root / '.github/actions/ai-direct-review/action.yml').read_text())
        self.approved = yaml.safe_load((root / '.github/workflows/owner-approved-ai-review.yml').read_text())

    def test_chunks_are_serial_and_observable_waits_for_final_claude_job(self):
        matrix = self.jobs['claude-review-chunks']['strategy']
        self.assertEqual(matrix['max-parallel'], 1)
        self.assertFalse(matrix['fail-fast'])
        self.assertIn('claude-code-plugin-review', self.jobs['observable-claude-review']['needs'])
        self.assertIn('!cancelled()', self.jobs['observable-claude-review']['if'])

    def test_claude_job_requires_publication_even_when_all_attempts_were_skipped(self):
        steps = {step.get('id'): step for step in self.jobs['claude-code-plugin-review']['steps']}
        step = steps['require_claude_completion']
        self.assertEqual(step['if'], 'always() && !cancelled()')
        self.assertNotIn('continue-on-error', step)
        for published, verified, expected in (('success', 'success', 0), ('skipped', 'skipped', 1), ('success', 'failure', 1), ('failure', 'skipped', 1)):
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', step['run']],
                                    env={**os.environ, 'PUBLICATION_OUTCOME': published, 'VERIFICATION_OUTCOME': verified}, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_owner_revision_is_used_for_tooling_and_other_authors_stay_on_main(self):
        checkout = next(step for step in self.action['runs']['steps'] if step['name'] == 'Check out trusted reviewer tooling')
        expression = checkout['with']['ref']
        self.assertIn('github.event.pull_request.user.login == github.repository_owner', expression)
        self.assertIn('github.event.pull_request.head.repo.full_name == github.repository', expression)
        self.assertIn('github.actor == github.repository_owner', expression)
        self.assertIn('github.triggering_actor == github.repository_owner', expression)
        self.assertIn('github.event.pull_request.head.sha', expression)
        self.assertIn("|| 'main'", expression)

    def test_preflight_runs_once_before_all_chunks(self):
        probes = [(job_id, step) for job_id, job in self.jobs.items() for step in job.get('steps', [])
                  if 'ai_review_preflight.py --probe claude' in step.get('run', '')]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0][0], 'claude-review-plan')
        sdk = self.jobs['claude-review-chunks']
        self.assertIn("needs.claude-review-plan.result == 'success'", sdk['if'])
        for step in sdk['steps']:
            if 'anthropics/claude-code-action@' in step.get('uses', ''):
                self.assertIn('needs.claude-review-plan.outputs.', step['with']['claude_args'])

    def test_owner_tooling_rule_resolves_all_untrusted_cases_to_main(self):
        owner = 'owner'
        sha = 'a' * 40
        base = {'github.event.pull_request.user.login': owner,
                'github.repository_owner': owner, 'github.actor': owner,
                'github.triggering_actor': owner, 'github.repository': 'owner/repo',
                'github.event.pull_request.head.repo.full_name': 'owner/repo',
                'github.event.pull_request.head.sha': sha}
        checkouts = [next(step for step in self.action['runs']['steps'] if step['name'] == 'Check out trusted reviewer tooling'),
                     next(step for step in self.approved['jobs']['review']['steps'] if step.get('id') == 'trusted_tooling')]
        for checkout in checkouts:
            expression = checkout['with']['ref'].removeprefix('${{ ').removesuffix(' }}')
            for override, expected in (({}, sha), ({'github.event.pull_request.user.login': 'contributor'}, 'main'),
                                       ({'github.event.pull_request.head.repo.full_name': 'fork/repo'}, 'main'),
                                       ({'github.actor': 'contributor'}, 'main'), ({'github.triggering_actor': 'contributor'}, 'main'),
                                       ({'github.event.pull_request.head.sha': ''}, 'main')):
                values = {**base, **override}
                # Resolve the actual checkout expression's restricted &&/||/==
                # grammar, without executing repository text or event values.
                alternatives = []
                for group in expression.split(' || '):
                    terms = []
                    for clause in group.split(' && '):
                        operands = clause.split(' == ')
                        operands = [values[token] if token in values else token.strip("'") for token in operands]
                        terms.append(operands[0] == operands[1] if len(operands) == 2 else operands[0])
                    alternatives.append(terms[-1] if all(terms) else False)
                actual = next(filter(None, alternatives))
                with self.subTest(checkout=checkout.get('id', 'action'), override=override):
                    self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()

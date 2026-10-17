"""Whole-diff Claude workflow, publication and tooling trust contracts."""
import os
from pathlib import Path
import re
import subprocess
import unittest

import yaml


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2]
        self.jobs = yaml.safe_load((root / '.github/workflows/pr-ai-review.yml').read_text())['jobs']
        self.action = yaml.safe_load((root / '.github/actions/ai-direct-review/action.yml').read_text())
        self.approved = yaml.safe_load((root / '.github/workflows/owner-approved-ai-review.yml').read_text())

    def test_single_claude_job_and_observable_run_sequentially(self):
        self.assertNotIn('claude-review-chunks', self.jobs)
        self.assertNotIn('claude-review-plan', self.jobs)
        self.assertNotIn('strategy', self.jobs['claude-code-plugin-review'])
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

    def test_preflight_runs_once_before_the_whole_diff_review(self):
        probes = [(job_id, step) for job_id, job in self.jobs.items() for step in job.get('steps', [])
                  if 'ai_review_preflight.py --probe claude' in step.get('run', '')]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0][0], 'claude-code-plugin-review')
        sdk = self.jobs['claude-code-plugin-review']
        self.assertEqual(sdk['needs'], 'direct-api-review')
        for step in sdk['steps']:
            if 'anthropics/claude-code-action@' in step.get('uses', ''):
                self.assertIn('steps.claude_models.outputs.', step['with']['claude_args'])

    def test_incomplete_diff_retries_even_when_sdk_attempt_failed(self):
        step_list = self.jobs['claude-code-plugin-review']['steps']
        steps = {step.get('id'): step for step in step_list}
        for step_id, action_id, readiness_id, prepare_id in (
            ('claude_review_primary_retry', 'claude_review_primary', 'primary_ready', 'prepare_claude_primary_finalize'),
            ('claude_review_fallback_retry', 'claude_review_fallback', 'fallback_ready', 'prepare_claude_fallback_finalize'),
        ):
            with self.subTest(step=step_id):
                condition = steps[step_id]['if']
                self.assertIn('!cancelled()', condition)
                self.assertIn(f"steps.claude_models.outputs.{readiness_id} == 'true'", condition)
                self.assertIn(f"steps.{prepare_id}.outputs.reason == 'diff_not_fully_read'", condition)
                self.assertIn(f"steps.extract_{action_id}.outcome != 'success'", condition)
                self.assertNotIn(f"steps.{action_id}.outcome == 'success'", condition)

        for step_id, action_id, prepare_id in (
            ('wait_primary_retry', 'claude_review_primary', 'prepare_claude_primary_finalize'),
            ('wait_fallback_retry', 'claude_review_fallback', 'prepare_claude_fallback_finalize'),
        ):
            expected_name = ('Wait before retrying the primary' if step_id == 'wait_primary_retry'
                             else 'Wait before retrying the fallback')
            step = next(step for step in step_list if step.get('name', '').startswith(expected_name))
            with self.subTest(step=step_id):
                condition = step['if']
                self.assertIn(f"steps.{prepare_id}.outputs.reason == 'diff_not_fully_read'", condition)
                self.assertNotIn(f"steps.{action_id}.outcome == 'success'", condition)

    def test_saved_execution_is_validated_after_action_failure_but_not_cancellation(self):
        steps = {step.get('id'): step for step in self.jobs['claude-code-plugin-review']['steps']}
        for route in ('primary', 'primary_retry', 'primary_finalize',
                      'fallback', 'fallback_retry', 'fallback_finalize'):
            action = 'claude_review_' + route
            condition = steps['extract_' + action]['if']
            for outcome, execution, cancelled, expected in (
                ('success', '/tmp/execution.json', False, True),
                ('failure', '/tmp/execution.json', False, True),
                ('skipped', '/tmp/execution.json', False, False),
                ('cancelled', '/tmp/execution.json', False, False),
                ('failure', '/tmp/execution.json', True, False),
                ('failure', '', False, False),
            ):
                values = {f'steps.{action}.outcome': outcome,
                          f'steps.{action}.outputs.execution_file': execution}
                terms = []
                for term in condition.split('&&'):
                    term = term.strip()
                    if term == 'always()':
                        terms.append(True)
                    elif term == '!cancelled()':
                        terms.append(not cancelled)
                    else:
                        match = re.fullmatch(r"(steps\.[\w.]+)\s*(==|!=)\s*'([^']*)'", term)
                        self.assertIsNotNone(match, term)
                        key, operator, literal = match.groups()
                        terms.append((values[key] == literal) == (operator == '=='))
                with self.subTest(route=route, outcome=outcome, execution=execution, cancelled=cancelled):
                    self.assertEqual(all(terms), expected)

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
                        operands = [token.strip().strip('() ') for token in operands]
                        operands = [values[token] if token in values else token.strip("'") for token in operands]
                        terms.append(operands[0] == operands[1] if len(operands) == 2 else operands[0])
                    alternatives.append(terms[-1] if all(terms) else False)
                actual = next(filter(None, alternatives))
                with self.subTest(checkout=checkout.get('id', 'action'), override=override):
                    self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()

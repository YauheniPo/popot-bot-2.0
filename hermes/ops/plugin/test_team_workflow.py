"""Team transitions use durable native results, never model-supplied verdicts."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parent / 'team-workflow' / 'engine.py'


class Backend:
    def __init__(self):
        self.calls = []
        self.records = {}
        self.stopped = []

    def spawn(self, brief, schema):
        ident = 'delegation-' + str(len(self.calls))
        self.calls.append(brief)
        self.records[ident] = {'origin_session': 'owner', 'state': 'running', 'result': None}
        return {'status': 'dispatched', 'delegation_id': ident, 'subagent_ids': ['child-' + ident]}

    def result(self, ident):
        return self.records.get(ident)

    def stop(self, ids):
        self.stopped.extend(ids)

    def finish(self, ident, verdict='pass', status='done', evidence=None):
        payload = {'status': status, 'result': 'fixture result',
                   'evidence': evidence if evidence is not None else ['checked fixture'],
                   'verdict': verdict, 'findings': [] if verdict == 'pass' else ['fixture defect with reproduction'],
                   'changed_files': []}
        self.records[ident].update(state='completed', result={'results': [{
            'status': 'completed', 'schema_valid': True, 'summary': json.dumps(payload),
        }]})


class ReceiptBackend(Backend):
    """Backend returning a caller-supplied native receipt."""

    def __init__(self, receipt):
        super().__init__()
        self.receipt = receipt

    def spawn(self, brief, schema):
        self.calls.append(brief)
        return self.receipt


class CodeBackend(Backend):
    def __init__(self, error=None):
        super().__init__()
        self.error = error
        self.worktrees = []

    def prepare_worktree(self, task_id, repository):
        self.worktrees.append((task_id, repository))
        if self.error:
            raise self.error
        return '/worktrees/' + task_id


def payload(**overrides):
    result = {'status': 'done', 'result': 'fixture result', 'evidence': ['checked fixture'],
              'verdict': 'not_reviewed', 'findings': [], 'changed_files': []}
    result.update(overrides)
    return result


def deliver(backend, job, summary):
    record = backend.records[job['delegation_id']]
    record.update(state='completed', result={'results': [{
        'status': 'completed', 'schema_valid': True,
        'summary': summary if isinstance(summary, str) else json.dumps(summary),
    }]})


class TeamWorkflowTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('team_engine', SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = Backend()
        self.clock = 100
        self.engine = module.Engine(Path(self.temp.name), self.backend, now=lambda: self.clock,
                                    policy={'max_revision_rounds': 2, 'deadline_seconds': 1000})

    def start(self, owner='owner', request='one'):
        return self.engine.call(owner, 'start', request_id=request, objective='Research a bounded question',
                                mode='research', acceptance='Cite primary sources', context='No mutations')

    def complete(self, job, **kwargs):
        self.backend.finish(job['delegation_id'], **kwargs)
        return self.engine.call('owner', 'advance', task_id=job['id'])

    def test_full_loop_and_duplicate_delivery_are_idempotent(self):
        job = self.start()
        self.assertEqual(self.start()['id'], job['id'])
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.engine.call('owner', 'advance', task_id=job['id'])['stage'], 'research')
        job = self.complete(job)
        self.assertEqual(job['stage'], 'execute')
        job = self.complete(job)
        self.assertEqual(job['stage'], 'review')
        job = self.complete(job, verdict='revise')
        self.assertEqual(job['stage'], 'execute')
        job = self.complete(job)
        job = self.complete(job)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(len(self.backend.calls), 5)
        self.engine.call('owner', 'advance', task_id=job['id'])
        self.assertEqual(len(self.backend.calls), 5)

    def test_context_survives_all_roles_without_automatic_memory_import(self):
        context = 'Language: Russian. Verified fact: fixture service uses loopback. Source: local config. No deploy.'
        job = self.engine.call('owner', 'start', request_id='context', objective='Inspect fixture',
                               mode='diagnostic', acceptance='Evidence only', context=context)
        job = self.complete(job)
        job = self.complete(job)
        for brief in self.backend.calls:
            self.assertIn(context, brief)
            self.assertIn('Do not assume shared memory', brief)
            self.assertIn('memory candidates', brief)
            self.assertIn('Do not write shared MEMORY.md or USER.md', brief)

    def test_owner_isolation_capacity_and_cancellation(self):
        job = self.start()
        with self.assertRaisesRegex(ValueError, 'owner'):
            self.engine.call('other', 'cancel', task_id=job['id'])
        with self.assertRaisesRegex(ValueError, 'busy'):
            self.start(owner='other', request='two')
        cancelled = self.engine.call('owner', 'cancel', task_id=job['id'])
        self.assertEqual(cancelled['status'], 'cancel_requested')
        self.assertTrue(self.backend.stopped)
        job = self.complete(job)
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(len(self.backend.calls), 1)

    def test_invalid_or_unverified_results_never_advance(self):
        for failure in ('bad_json', 'missing_evidence', 'iteration_limit', 'summary_truncated'):
            with self.subTest(failure=failure):
                job = self.start(request=failure)
                self.backend.finish(job['delegation_id'])
                record = self.backend.records[job['delegation_id']]
                if failure == 'bad_json':
                    record['result']['results'][0]['summary'] = 'looks good'
                elif failure == 'missing_evidence':
                    self.backend.finish(job['delegation_id'], evidence=[])
                else:
                    record['result']['results'][0][
                        'truncated' if failure == 'iteration_limit' else 'summary_truncated'] = True
                result = self.engine.call('owner', 'advance', task_id=job['id'])
                self.assertEqual(result['status'], 'blocked')

    def test_deadline_cancels_owned_children_without_starting_next_stage(self):
        job = self.start()
        self.clock += 1001
        result = self.engine.call('owner', 'advance', task_id=job['id'])
        self.assertEqual(result['status'], 'cancel_requested')
        self.assertEqual(result['reason'], 'deadline')
        result = self.complete(job)
        self.assertEqual(result['status'], 'timed_out')
        self.assertEqual(len(self.backend.calls), 1)

    def test_review_budget_is_finite(self):
        job = self.start()
        job = self.complete(job)
        for _ in range(3):
            job = self.complete(job)
            job = self.complete(job, verdict='revise')
        self.assertEqual(job['status'], 'needs_input')
        self.assertEqual(len(self.backend.calls), 7)

    def test_restart_recovers_state_but_never_replays_unknown_dispatch(self):
        job = self.start()
        self.backend.records[job['delegation_id']].update(state='error', result={'error': 'shutdown'})
        self.engine = type(self.engine)(self.engine.root, self.backend, now=lambda: self.clock,
                                       policy={'max_revision_rounds': 2, 'deadline_seconds': 1000})
        result = self.engine.call('owner', 'advance', task_id=job['id'])
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(len(self.backend.calls), 1)

    def test_missing_or_foreign_receipt_keeps_capacity_reserved(self):
        job = self.start()
        self.backend.records[job['delegation_id']]['origin_session'] = 'other'
        result = self.engine.call('owner', 'advance', task_id=job['id'])
        self.assertEqual(result['status'], 'dispatch_unknown')
        with self.assertRaisesRegex(ValueError, 'busy'):
            self.start(request='new')
        self.engine.call('owner', 'cancel', task_id=job['id'])
        self.assertFalse(self.backend.stopped)

    def test_lost_dispatch_receipt_is_not_replayed(self):
        with patch.object(self.backend, 'spawn', side_effect=TimeoutError):
            job = self.start()
        self.assertEqual(job['status'], 'dispatch_unknown')
        self.assertEqual(self.start()['id'], job['id'])
        self.engine.call('owner', 'advance', task_id=job['id'])
        self.assertEqual(len(self.backend.calls), 0)

    def test_native_unknown_after_restart_requires_operator_inspection(self):
        job = self.start()
        self.backend.records[job['delegation_id']]['state'] = 'unknown'
        result = self.engine.call('owner', 'advance', task_id=job['id'])
        self.assertEqual(result['status'], 'dispatch_unknown')
        self.assertIn('Operator', result['next_action'])
        with self.assertRaisesRegex(ValueError, 'busy'):
            self.start(request='new')

    def test_missing_native_record_never_releases_capacity(self):
        job = self.start()
        del self.backend.records[job['delegation_id']]
        result = self.engine.call('owner', 'advance', task_id=job['id'])
        self.assertEqual(result['status'], 'dispatch_unknown')
        self.assertEqual(len(self.backend.calls), 1)

    def test_request_key_cannot_silently_reuse_different_task(self):
        self.start()
        with self.assertRaisesRegex(ValueError, 'different task'):
            self.engine.call('owner', 'start', request_id='one', objective='Something else')

    def test_status_reports_result_ready_without_spawning_next_stage(self):
        job = self.start()
        self.backend.finish(job['delegation_id'])
        result = self.engine.call('owner', 'status', task_id=job['id'])
        self.assertEqual(result['status'], 'awaiting_coordinator')
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.engine.call('owner', 'advance', task_id=job['id'])['stage'], 'execute')

    def test_finalizing_is_not_completed_and_cannot_release_capacity(self):
        job = self.start()
        self.backend.records[job['delegation_id']]['state'] = 'finalizing'
        self.engine.call('owner', 'cancel', task_id=job['id'])
        result = self.engine.call('owner', 'status', task_id=job['id'])
        self.assertEqual(result['status'], 'cancel_requested')
        with self.assertRaisesRegex(ValueError, 'busy'):
            self.start(request='new')

    def test_two_controllers_cannot_dispatch_concurrently(self):
        second = type(self.engine)(self.engine.root, self.backend,
                                   policy={'max_revision_rounds': 2, 'deadline_seconds': 1000})
        original = self.backend.spawn

        def spawn(brief, schema):
            with self.assertRaisesRegex(ValueError, 'busy'):
                second.call('owner', 'start', request_id='duplicate')
            return original(brief, schema)

        with patch.object(self.backend, 'spawn', side_effect=spawn):
            self.start()
        self.assertEqual(len(self.backend.calls), 1)

    def test_synchronous_native_fallback_requests_immediate_advance(self):
        self.backend.records['inline'] = {}
        self.backend.finish('inline')
        receipt = self.backend.records['inline']['result']
        with patch.object(self.backend, 'spawn', return_value=receipt):
            job = self.start()
        self.assertIn('now', job['next_action'])
        result = self.engine.call('owner', 'advance', task_id=job['id'])
        self.assertEqual(result['stage'], 'execute')

    def build(self, root, backend, rounds=2, seconds=1000):
        return type(self.engine)(root, backend, now=lambda: self.clock,
                                 policy={'max_revision_rounds': rounds, 'deadline_seconds': seconds})

    def start_code(self, engine, request='code'):
        return engine.call('owner', 'start', request_id=request, objective='Implement a bounded change',
                           acceptance='Tests pass', context='Only task files', mode='code',
                           repository='/managed/repository')

    def test_state_directory_permissions_and_state_symlink_guard(self):
        symlink = Path(self.temp.name) / 'linked-state'
        symlink.symlink_to(self.engine.root)
        with self.assertRaisesRegex(ValueError, 'Team state symlink is not allowed'):
            self.build(symlink, self.backend)
        fresh = Path(self.temp.name) / 'fresh-state'
        engine = self.build(fresh, self.backend)
        self.assertEqual((fresh.stat().st_mode & 0o777), 0o700)
        self.assertEqual(engine.call('owner', 'list'), [])

    def test_evidence_items_must_be_bounded_text(self):
        for value in ("not-a-list", ["ok", "x" * 401], [None]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.engine._validate_items({'evidence': value}, 'evidence')

    def test_policy_bounds_and_required_text_limits(self):
        for rounds in (6, -1, True, '2'):
            with self.subTest(rounds=rounds), self.assertRaisesRegex(ValueError, 'Invalid revision budget'):
                self.build(Path(self.temp.name) / 'policy', self.backend, rounds=rounds)
        for seconds in (29, 7201, True, '300'):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(ValueError, 'Invalid team deadline'):
                self.build(Path(self.temp.name) / 'policy', self.backend, seconds=seconds)
        with self.assertRaisesRegex(ValueError, 'session owner must be non-empty text, at most 1000'):
            self.engine.call('   ', 'list')
        with self.assertRaisesRegex(ValueError, 'objective must be non-empty text, at most 6000'):
            self.engine.call('owner', 'start', request_id='long-objective', objective='x' * 6001,
                             acceptance='Acceptance', context='Context', mode='research')
        with self.assertRaisesRegex(ValueError, 'request_id must be non-empty text, at most 100'):
            self.engine.call('owner', 'start', request_id=None, objective='Objective',
                             acceptance='Acceptance', context='Context', mode='research')
        self.assertEqual(self.backend.calls, [])

    def test_state_file_integrity_guards_reject_tampered_records(self):
        root = Path(self.temp.name) / 'guards'
        engine = self.build(root, self.backend)
        ident = engine.call('owner', 'start', request_id='guards', objective='Objective',
                            acceptance='Acceptance', context='Context', mode='research')['id']
        path = root / (ident + '.json')
        original = path.read_text()
        for name, message in (('identity', 'Invalid team state identity'),
                              ('filename', 'Team state filename mismatch'),
                              ('hardlink', 'Team state hardlink is not allowed')):
            with self.subTest(name=name):
                path.write_text(original)
                if name == 'hardlink':
                    link = root / ('b' * 32 + '.json')
                    os.link(path, link, follow_symlinks=False)
                    with self.assertRaisesRegex(ValueError, message):
                        engine._read(link)
                    link.unlink()
                    continue
                job = json.loads(path.read_text())
                if name == 'identity':
                    job['id'] = 'not-a-team-id'
                    path.write_text(json.dumps(job))
                    with self.assertRaisesRegex(ValueError, message):
                        engine._read(path)
                    continue
                renamed = root / 'renamed.json'
                renamed.write_text(json.dumps(job))
                with self.assertRaisesRegex(ValueError, message):
                    engine._read(renamed)
        self.assertEqual(json.loads(path.read_text())['id'], ident)

    def test_history_limit_unknown_task_and_unknown_action_are_rejected(self):
        for index in range(1001):
            (self.engine.root / f'{index:032x}.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Team history limit reached'):
            self.engine.call('owner', 'list')
        for path in self.engine.root.glob('*.json'):
            path.unlink()
        job = self.start()
        with self.assertRaisesRegex(ValueError, 'Unknown task or wrong owner'):
            self.engine.call('owner', 'status', task_id='f' * 32)
        with self.assertRaisesRegex(ValueError, 'Unknown team action'):
            self.engine.call('owner', 'replace', task_id=job['id'])
        self.assertEqual(self.engine.call('owner', 'status', task_id=job['id'])['stage'], 'research')
        self.assertEqual(len(self.backend.calls), 1)

    def test_unfinished_task_holds_capacity_for_every_owner(self):
        job = self.start()
        with self.assertRaisesRegex(ValueError, 'Team busy with another task; no work was queued or started'):
            self.start(owner='owner', request='second')
        with self.assertRaisesRegex(ValueError, 'Team busy with another task; no work was queued or started'):
            self.start(owner='someone-else', request='other')
        self.assertEqual([item['id'] for item in self.engine.call('owner', 'list')], [job['id']])
        self.assertEqual(self.engine.call('someone-else', 'list'), [])

    def test_unsupported_mode_and_missing_identity_are_rejected_before_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported task mode'):
            self.engine.call('owner', 'start', request_id='mode', objective='Objective',
                             acceptance='Acceptance', context='Context', mode='research-and-review')
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.engine.call('owner', 'list'), [])

    def test_code_mode_prepares_a_worktree_before_dispatch(self):
        backend = CodeBackend()
        engine = self.build(self.engine.root, backend)
        job = self.start_code(engine)
        self.assertEqual(backend.worktrees, [(job['id'], '/managed/repository')])
        self.assertEqual(job['worktree'], '/worktrees/' + job['id'])
        self.assertIn('/worktrees/' + job['id'], backend.calls[0])
        self.assertEqual(job['status'], 'running')

    def test_worktree_failure_blocks_the_task_without_dispatch(self):
        backend = CodeBackend(error=ValueError('Worktree target already exists or is unsafe'))
        engine = self.build(self.engine.root, backend)
        job = self.start_code(engine)
        self.assertEqual(job['status'], 'blocked')
        self.assertEqual(job['reason'], 'Dispatch/setup failed; inspect task and native delegation state')
        self.assertIsNone(job['worktree'])
        self.assertEqual(backend.calls, [])
        self.assertIn('do not imply external delivery is confirmed', job['next_action'].lower())
        self.assertEqual(backend.worktrees, [(job['id'], '/managed/repository')])

    def test_unsafe_artifact_directory_is_rejected(self):
        backend = CodeBackend()
        engine = self.build(self.engine.root, backend)
        artifacts = Path(str(self.engine.root)) / 'artifacts'
        real_is_symlink = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path.parent == artifacts or real_is_symlink(path)):
            job = engine.call('owner', 'start', request_id='artifacts', objective='Objective',
                              acceptance='Acceptance', context='Context', mode='research')
        self.assertEqual(job['status'], 'blocked')
        self.assertEqual(job['reason'], 'Dispatch/setup failed; inspect task and native delegation state')
        self.assertEqual(backend.calls, [])
        self.assertFalse(artifacts.exists())

    def test_receipt_without_a_usable_identity_requires_inspection(self):
        backend = ReceiptBackend({'status': 'dispatched', 'delegation_id': None})
        engine = self.build(self.engine.root, backend)
        job = engine.call('owner', 'start', request_id='receipt', objective='Objective',
                          acceptance='Acceptance', context='Context', mode='research')
        self.assertEqual(job['status'], 'dispatch_unknown')
        self.assertEqual(job['reason'], 'No usable native dispatch receipt; inspect before retrying')
        self.assertIn('Operator', job['next_action'])
        self.assertEqual(len(backend.calls), 1)

    def test_completed_stage_with_needs_input_outcome_stops_the_loop(self):
        job = self.start()
        deliver(self.backend, job, payload(status='needs_input', result='Owner must supply the missing credential'))
        result = self.engine.call('owner', 'advance', task_id=job['id'])
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['reason'], 'Owner must supply the missing credential')
        self.assertEqual(result['stage'], 'research')
        self.assertEqual(len(self.backend.calls), 1)
        self.assertIn('Report outcome', self.engine.call('owner', 'list')[0]['next_action'])

    def test_invalid_stage_payloads_are_blocked_with_the_exact_reason(self):
        cases = (
            ('not-an-object', ['not', 'an', 'object']),
            ('extra-key', {**payload(), 'extra': 'value'}),
            ('invalid-outcome', payload(status='maybe')),
            ('invalid-evidence', payload(evidence='checked')),
            ('missing-evidence', payload(evidence=[])),
            ('invalid-verdict', payload(verdict='approved')),
            ('oversized-item', payload(evidence=['x' * 401])),
            ('too-many-findings', payload(findings=['finding'] * 13)),
        )
        for name, summary in cases:
            with self.subTest(name=name):
                job = self.start(request=name)
                before = len(self.backend.calls)
                deliver(self.backend, job, summary)
                result = self.engine.call('owner', 'advance', task_id=job['id'])
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['reason'],
                                 'Stage failed or returned invalid/unverified evidence; inspect native result')
                self.assertEqual(result['stage'], 'research')
                self.assertEqual(result['results'], [])
                self.assertEqual(len(self.backend.calls), before)

    def test_reviewer_payload_must_match_the_review_stage_contract(self):
        inconsistent = (({'status': 'done', 'result': 'Looks fine', 'evidence': ['read the diff'],
                          'verdict': 'pass', 'findings': ['still broken'], 'changed_files': []},
                         'pass with findings'),
                        ({'status': 'done', 'result': 'Needs work', 'evidence': ['read the diff'],
                          'verdict': 'revise', 'findings': [], 'changed_files': []},
                         'revise without findings'),
                        ({'status': 'done', 'result': 'Not reviewed', 'evidence': ['read the diff'],
                          'verdict': 'not_reviewed', 'findings': [], 'changed_files': []},
                         'not_reviewed review'))
        for summary, name in inconsistent:
            with self.subTest(name=name):
                job = self.start(request=name)
                for _ in range(2):
                    job = self.complete(job)
                self.assertEqual(job['stage'], 'review')
                before = len(self.backend.calls)
                deliver(self.backend, job, summary)
                result = self.engine.call('owner', 'advance', task_id=job['id'])
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['reason'],
                                 'Stage failed or returned invalid/unverified evidence; inspect native result')
                self.assertEqual(result['stage'], 'review')
                self.assertEqual(len(self.backend.calls), before)

    def test_blocked_and_needs_input_stages_stop_the_loop(self):
        for status, reason in (('blocked', 'Native stage could not reach the artifact'),
                               ('needs_input', 'Owner decision required for the remaining action')):
            with self.subTest(status=status):
                job = self.start(request=status)
                before = len(self.backend.calls)
                deliver(self.backend, job, payload(status=status, result=reason))
                result = self.engine.call('owner', 'advance', task_id=job['id'])
                self.assertEqual(result['status'], status)
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['stage'], 'research')
                self.assertEqual(len(self.backend.calls), before)
                self.assertEqual(self.engine.call('owner', 'cancel', task_id=job['id'])['status'], status)

    def test_crash_between_intent_and_receipt_is_never_replayed(self):
        job = self.start()
        path = self.engine.root / (job['id'] + '.json')
        saved = json.loads(path.read_text())
        saved.update(status='dispatching', delegation_id=None, subagent_ids=[])
        path.write_text(json.dumps(saved))
        result = self.engine.call('owner', 'status', task_id=job['id'])
        self.assertEqual(result['status'], 'dispatch_unknown')
        self.assertEqual(result['reason'],
                         'Unconfirmed dispatch; inspect native delegation before cancelling')
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.engine.call('owner', 'advance', task_id=job['id'])['status'], 'dispatch_unknown')
        self.assertEqual(len(self.backend.calls), 1)

    def test_deadline_on_a_finished_native_stage_times_out_without_a_new_stage(self):
        job = self.start()
        self.backend.finish(job['delegation_id'])
        self.clock += 1001
        result = self.engine.call('owner', 'advance', task_id=job['id'])
        self.assertEqual(result['status'], 'timed_out')
        self.assertEqual(result['reason'], 'deadline')
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.engine.call('owner', 'list')[0]['status'], 'timed_out')


if __name__ == '__main__':
    unittest.main()

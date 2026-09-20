"""Team transitions use durable native results, never model-supplied verdicts."""
import importlib.util
import json
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


if __name__ == '__main__':
    unittest.main()

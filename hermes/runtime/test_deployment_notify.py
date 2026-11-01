"""Local subprocess tests for bounded, secret-safe deployment notifications."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock


HELPER = Path(__file__).with_name('deployment-notify.py')
TOKEN = '123:test-only-token'
MESSAGE = 'Deploy "status"\nПривет'
FAKE_CURL = '''
import json, os, sys, time
from pathlib import Path
from urllib.parse import parse_qs
if os.environ.get('NOTIFY_TEST_NO_READ'):
    with Path(os.environ['NOTIFY_TEST_LOG']).open('a') as handle:
        handle.write(json.dumps({'kind': 'start', 'time': time.monotonic(), 'pid': os.getpid(),
                                 'recipient': 'non-reader', 'argv': sys.argv[1:]}) + '\\n')
    time.sleep(60)
config = dict(line.split(' = ', 1) for line in sys.stdin.read().splitlines())
url, data = (json.loads(config[key]) for key in ('url', 'data'))
body = parse_qs(data)
recipient = body['chat_id'][0]
assert url == 'https://api.telegram.org/bot123:test-only-token/sendMessage'
assert body['text'][0] == 'Deploy "status"\\nПривет'
log = Path(os.environ['NOTIFY_TEST_LOG'])
def record(kind):
    with log.open('a') as handle:
        handle.write(json.dumps({'kind': kind, 'time': time.monotonic(), 'pid': os.getpid(),
                                 'recipient': recipient, 'argv': sys.argv[1:]}) + '\\n')
record('start')
workers = int(os.environ.get('NOTIFY_TEST_WORKERS', '3'))
total = int(os.environ.get('NOTIFY_TEST_TOTAL', '3'))
if os.environ.get('NOTIFY_TEST_BARRIER'):
    started = sum(json.loads(line)['kind'] == 'start' for line in log.read_text().splitlines())
    target = min(total, ((started - 1) // workers + 1) * workers)
    until = time.monotonic() + 2
    while time.monotonic() < until:
        count = sum(json.loads(line)['kind'] == 'start' for line in log.read_text().splitlines())
        if count >= target:
            break
        time.sleep(.01)
time.sleep(float(os.environ.get('NOTIFY_TEST_DELAY', '.05')))
record('end')
if os.environ.get('NOTIFY_TEST_FAILURES') and recipient == '3':
    sys.exit(28)
print('500' if os.environ.get('NOTIFY_TEST_FAILURES') and recipient == '2' else '200', end='')
'''


class DeploymentNotifyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(HELPER.is_file(), 'The deploy batch notification helper is required')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        curl = self.root / 'curl'
        curl.write_text('#!' + sys.executable + '\n' + FAKE_CURL)
        curl.chmod(0o755)
        self.log = self.root / 'requests.jsonl'

    def start(self, count=3, workers=3, timeout=3, **environment):
        command = [sys.executable, str(HELPER), '--timeout', str(timeout), '--workers', str(workers)]
        env = {**os.environ, 'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
               'NOTIFY_TEST_LOG': str(self.log), 'NOTIFY_TEST_TOTAL': str(count),
               'NOTIFY_TEST_WORKERS': str(workers), **environment}
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
        payload = json.dumps({'token': TOKEN, 'chat_ids': [str(i + 1) for i in range(count)], 'text': MESSAGE})
        return process, payload

    def stop(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        for handle in (process.stdin, process.stdout, process.stderr):
            if handle and not handle.closed:
                handle.close()

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_configured_concurrency_waits_for_every_recipient_without_secret_arguments(self):
        process, payload = self.start(count=5, workers=2, NOTIFY_TEST_BARRIER='1')
        try:
            stdout, stderr = process.communicate(payload, timeout=12)
        finally:
            self.stop(process)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout), {'sent': 5, 'failed': 0})
        events = self.events()
        active, maximum = 0, 0
        for event in sorted(events, key=lambda item: item['time']):
            active += 1 if event['kind'] == 'start' else -1
            maximum = max(maximum, active)
            self.assertNotIn(TOKEN, ' '.join(event['argv']))
            self.assertNotIn(MESSAGE, ' '.join(event['argv']))
            self.assertEqual(event['argv'][0], '--disable')
            self.assertEqual(event['argv'][event['argv'].index('--proto') + 1], '=https')
            self.assertNotIn('--insecure', event['argv'])
            self.assertNotIn('--location', event['argv'])
            self.assertEqual(event['argv'][event['argv'].index('--max-time') + 1], '3.0')
        self.assertEqual(maximum, 2)
        self.assertEqual(active, 0)
        self.assertCountEqual([e['recipient'] for e in events if e['kind'] == 'start'], list('12345'))
        self.assertNotIn(TOKEN, stdout + stderr)
        self.assertNotIn(MESSAGE, stdout + stderr)

    def test_http_and_transport_failures_are_counted_without_retrying(self):
        process, payload = self.start(NOTIFY_TEST_FAILURES='1', NOTIFY_TEST_BARRIER='1')
        try:
            stdout, stderr = process.communicate(payload, timeout=8)
        finally:
            self.stop(process)
        self.assertEqual(process.returncode, 1, stderr)
        self.assertEqual(json.loads(stdout), {'sent': 1, 'failed': 2})
        self.assertEqual(sum(e['kind'] == 'start' for e in self.events()), 3)
        self.assertNotIn(TOKEN, stdout + stderr)

    def test_owner_deadline_kills_and_reaps_workers_that_ignore_request_timeout(self):
        started = time.monotonic()
        process, payload = self.start(timeout=.1, NOTIFY_TEST_DELAY='60')
        try:
            stdout, stderr = process.communicate(payload, timeout=6)
        finally:
            self.stop(process)
        self.assertEqual(process.returncode, 1, stderr)
        self.assertEqual(json.loads(stdout), {'sent': 0, 'failed': 3})
        self.assertLess(time.monotonic() - started, 5)
        self.assert_reaped()

    def assert_reaped(self):
        for event in self.events():
            if event['kind'] == 'start':
                with self.assertRaises(ProcessLookupError):
                    os.kill(event['pid'], 0)

    def test_deadline_also_bounds_a_worker_that_never_reads_its_input(self):
        process, payload = self.start(timeout=.1, NOTIFY_TEST_NO_READ='1')
        payload = json.loads(payload)
        payload['text'] = 'x' * 100000
        try:
            stdout, stderr = process.communicate(json.dumps(payload), timeout=5)
        finally:
            self.stop(process)
        self.assertEqual(process.returncode, 1, stderr)
        self.assertEqual(json.loads(stdout), {'sent': 0, 'failed': 3})
        self.assert_reaped()

    def test_termination_reaps_active_workers_and_does_not_start_next_wave(self):
        process, payload = self.start(count=5, NOTIFY_TEST_DELAY='60')
        try:
            process.stdin.write(payload)
            process.stdin.close()
            process.stdin = None
            until = time.monotonic() + 5
            while time.monotonic() < until:
                if self.log.exists() and len(self.events()) == 3:
                    break
                time.sleep(.01)
            self.assertTrue(self.log.exists())
            self.assertEqual(len(self.events()), 3)
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr)
            self.assertEqual(len(self.events()), 3)
            self.assertNotIn(TOKEN, stdout + stderr)
            self.assert_reaped()
        finally:
            self.stop(process)


def load_helper():
    """Import the helper file into this process so coverage sees its lines."""
    spec = importlib.util.spec_from_file_location('deployment_notify', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeploymentNotifyInProcessTests(unittest.TestCase):
    """Run the helper inside the test process so CI coverage sees its lines."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        curl = self.root / 'curl'
        curl.write_text('#!' + sys.executable + '\n' + FAKE_CURL)
        curl.chmod(0o755)
        self.log = self.root / 'requests.jsonl'
        self.environment = {
            'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
            'NOTIFY_TEST_LOG': str(self.log),
            'NOTIFY_TEST_TOTAL': '3',
            'NOTIFY_TEST_WORKERS': '2',
        }

    def payload(self, chat_ids=None):
        return json.dumps({
            'token': TOKEN,
            'chat_ids': chat_ids if chat_ids is not None else ['1', '2', '3'],
            'text': MESSAGE,
        })

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def call_main(self, argv, stdin_text, environment=None):
        helper = load_helper()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with mock.patch.dict(os.environ, {**self.environment, **(environment or {})}, clear=False):
                with mock.patch.object(sys, 'argv', argv):
                    with mock.patch.object(sys, 'stdin', io.StringIO(stdin_text)):
                        try:
                            code = helper.main()
                        except SystemExit as error:
                            code = error.code
        return SimpleNamespace(returncode=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())

    def test_send_reports_every_recipient_without_secret_arguments(self):
        helper = load_helper()
        with mock.patch.dict(os.environ, self.environment, clear=False):
            result = helper.send(json.loads(self.payload(['1', ' 2 ', '', '3'])), 3, 2)
        self.assertEqual(result, {'sent': 3, 'failed': 0})
        events = self.events()
        self.assertEqual(len(events), 6)
        for event in events:
            self.assertNotIn(TOKEN, ' '.join(event['argv']))
            self.assertNotIn(MESSAGE, ' '.join(event['argv']))

    def test_send_counts_http_and_transport_failures(self):
        helper = load_helper()
        env = {**self.environment, 'NOTIFY_TEST_FAILURES': '1'}
        with mock.patch.dict(os.environ, env, clear=False):
            result = helper.send(json.loads(self.payload()), 3, 2)
        self.assertEqual(result, {'sent': 1, 'failed': 2})
        self.assertEqual(sum(e['kind'] == 'start' for e in self.events()), 3)

    def test_deadline_kills_a_worker_that_ignores_the_timeout(self):
        helper = load_helper()
        env = {**self.environment, 'NOTIFY_TEST_DELAY': '60'}
        started = time.monotonic()
        with mock.patch.dict(os.environ, env, clear=False):
            result = helper.send(json.loads(self.payload()), 0.1, 2)
        self.assertEqual(result, {'sent': 0, 'failed': 3})
        self.assertLess(time.monotonic() - started, 5)
        for event in self.events():
            if event['kind'] == 'start':
                with self.assertRaises(ProcessLookupError):
                    os.kill(event['pid'], 0)

    def test_failed_process_start_reaps_running_workers(self):
        helper = load_helper()
        real_popen = subprocess.Popen
        calls = []

        def flaky_popen(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise FileNotFoundError('curl vanished')
            process = real_popen(*args, **kwargs)
            # Wait until the child records its start so the reap below is
            # observable instead of racing the child's first log write.
            until = time.monotonic() + 5
            while time.monotonic() < until:
                if self.log.exists() and self.log.read_text().strip():
                    break
                time.sleep(.01)
            return process

        # NO_READ makes the child record its start immediately, before the
        # second process start fails and the finally block reaps it.
        env = {**self.environment, 'NOTIFY_TEST_NO_READ': '1'}
        payload = json.loads(self.payload())
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(subprocess, 'Popen', flaky_popen):
                with self.assertRaises(FileNotFoundError):
                    helper.send(payload, 3, 2)
        self.assertEqual(len(calls), 2)
        events = [e for e in self.events() if e['kind'] == 'start']
        self.assertEqual(len(events), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(events[0]['pid'], 0)

    def test_main_prints_counts_and_exits_zero_on_success(self):
        result = self.call_main([str(HELPER), '--timeout', '3', '--workers', '2'], self.payload())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'sent': 3, 'failed': 0})
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertNotIn(MESSAGE, result.stdout + result.stderr)

    def test_main_exits_nonzero_when_any_recipient_fails(self):
        result = self.call_main([str(HELPER), '--timeout', '3', '--workers', '2'],
                                self.payload(), environment={'NOTIFY_TEST_FAILURES': '1'})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'sent': 1, 'failed': 2})

    def test_main_rejects_nonpositive_or_nonfinite_arguments(self):
        for timeout, workers in (('0', '2'), ('-1', '2'), ('nan', '2'), ('inf', '2'),
                                 ('2', '0'), ('2', '-2')):
            with self.subTest(timeout=timeout, workers=workers):
                result = self.call_main(
                    [str(HELPER), '--timeout', timeout, '--workers', workers], self.payload())
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('"sent"', result.stdout)

    def test_main_reports_unusable_payloads_without_secrets(self):
        payloads = ('not json', '{}',
                    json.dumps({'token': TOKEN, 'chat_ids': 5, 'text': MESSAGE}))
        for stdin_text in payloads:
            with self.subTest(payload=stdin_text[:20]):
                result = self.call_main(
                    [str(HELPER), '--timeout', '3', '--workers', '2'], stdin_text)
                self.assertEqual(result.returncode, 1)
                self.assertIn('batch failed', result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertNotIn(TOKEN, result.stderr)

    def test_main_reports_failed_child_start_without_secrets(self):
        def failing_popen(*args, **kwargs):
            raise FileNotFoundError('curl vanished')

        with mock.patch.object(subprocess, 'Popen', failing_popen):
            result = self.call_main([str(HELPER), '--timeout', '3', '--workers', '2'],
                                    self.payload())
        self.assertEqual(result.returncode, 1)
        self.assertIn('batch failed', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_module_entry_point_runs_main(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with mock.patch.dict(os.environ, self.environment, clear=False):
                with mock.patch.object(sys, 'argv', [str(HELPER), '--timeout', '3', '--workers', '2']):
                    with mock.patch.object(sys, 'stdin', io.StringIO(self.payload())):
                        with self.assertRaises(SystemExit) as raised:
                            runpy.run_path(str(HELPER), run_name='__main__')
        self.assertEqual(raised.exception.code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue()), {'sent': 3, 'failed': 0})

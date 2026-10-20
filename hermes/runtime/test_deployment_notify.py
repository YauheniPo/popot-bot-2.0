"""Local subprocess tests for bounded, secret-safe deployment notifications."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


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

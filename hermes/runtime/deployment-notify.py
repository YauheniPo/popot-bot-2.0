#!/usr/bin/env python3
"""Send one bounded foreground batch; credentials stay in stdin, not argv."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import signal
import subprocess
import sys
import time
from urllib.parse import urlencode


def communicate(process, config, deadline):
    with process:
        try:
            output, _ = process.communicate(input=config, timeout=max(.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        return process.returncode == 0 and output == b'200'


def send(payload, timeout, workers):
    recipients = [str(value).strip() for value in payload['chat_ids'] if str(value).strip()]
    sent = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for offset in range(0, len(recipients), workers):
            active, results = [], []
            deadline = time.monotonic() + timeout + 1
            try:
                for recipient in recipients[offset:offset + workers]:
                    config = '\n'.join([
                        'url = ' + json.dumps('https://api.telegram.org/bot' + payload['token'] + '/sendMessage'),
                        'data = ' + json.dumps(urlencode({'chat_id': recipient, 'text': payload['text']})),
                    ]) + '\n'
                    process = subprocess.Popen([
                        'curl', '--disable', '--silent', '--proto', '=https', '--tlsv1.2',
                        '--output', '/dev/null', '--write-out', '%{http_code}',
                        '--max-time', str(timeout), '--config', '-',
                    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    active.append(process)
                    results.append(pool.submit(communicate, process, config.encode(), deadline))
                sent += sum(result.result() for result in results)
            finally:
                # Also reap every child on SIGTERM, Ctrl-C, or a failed process start.
                for process in active:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
    return {'sent': sent, 'failed': len(recipients) - sent}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, required=True)
    parser.add_argument('--workers', type=int, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.workers <= 0:
        parser.error('timeout and workers must be positive finite values')
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    try:
        result = send(json.load(sys.stdin), args.timeout, args.workers)
    except (OSError, ValueError, KeyError, TypeError):
        print('Deployment notification batch failed; credentials and payload omitted.', file=sys.stderr)
        return 1
    print(json.dumps(result))
    return int(result['failed'] > 0)


if __name__ == '__main__':
    sys.exit(main())

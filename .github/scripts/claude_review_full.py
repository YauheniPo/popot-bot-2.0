#!/usr/bin/env python3
"""Prepare one complete SDK review and require exact full-diff read evidence."""
import argparse
import json
import os
from pathlib import Path
import subprocess

import claude_review_finalize as finalize
import pr_review_context as context


def exact_diff():
    return subprocess.run(
        ['git', '-c', 'core.quotePath=false', 'diff', '--no-ext-diff', '--no-textconv',
         '--find-renames', '--unified=3', context._required_commit_sha('BASE_SHA'),
         context._required_commit_sha('HEAD_SHA'), '--', '.'],
        cwd=Path.cwd(), capture_output=True, check=True, encoding='utf-8', timeout=30,
    ).stdout


def prepare():
    diff = exact_diff()
    if not 0 < len(diff.encode('utf-8')) <= context.MAX_CLAUDE_EXECUTION_FILE_BYTES:
        raise RuntimeError('invalid_diff_size')
    (Path.cwd() / '.ci-pr-review.diff').write_text(diff, encoding='utf-8')
    return {'read_plan': finalize.read_plan()}


def extract(execution_file, output_file):
    events = finalize._root_events(context._claude_execution_events(execution_file))
    _, result = finalize._session(events)
    if result.get('subtype') != 'success' or result.get('is_error') is not False:
        raise RuntimeError('review_execution_failed')
    path, _, lines = finalize._diff()
    try:
        coverage = finalize._require_complete_diff(events, path, lines)
    except finalize.IncompleteDiffRead as error:
        print('Claude full diff-read coverage: ' + json.dumps(error.coverage))
        raise
    context._command_extract(execution_file, output_file)
    print('Claude full diff-read coverage: ' + json.dumps(coverage))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'extract'))
    parser.add_argument('--execution-file', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'prepare':
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as output:
            for key, value in prepare().items():
                output.write(f'{key}={json.dumps(value, separators=(",", ":"))}\n')
    elif args.execution_file is None or args.output is None:
        parser.error('extract requires --execution-file and --output')
    else:
        extract(args.execution_file, args.output)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit('Claude review validation failed: ' + context.review_failure_code(error)) from None

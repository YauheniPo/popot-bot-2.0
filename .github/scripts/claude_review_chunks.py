#!/usr/bin/env python3
"""Plan serial Claude SDK chunks and publish only a complete revision-bound set."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import claude_review_finalize as finalize
import pr_review_context as context
from review_execution import PUBLIC_ENDPOINTS, safe_label


def _limit(name, default, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise RuntimeError('invalid_chunk_limit') from None
    if not 1 <= value <= maximum:
        raise RuntimeError('invalid_chunk_limit')
    return value


def exact_diff():
    return subprocess.run(
        ['git', 'diff', '--no-ext-diff', '--no-textconv', '--find-renames', '--unified=20',
         context._required_commit_sha('BASE_SHA'), context._required_commit_sha('HEAD_SHA'), '--', '.'],
        cwd=Path.cwd(), capture_output=True, check=True, encoding='utf-8', timeout=30,
    ).stdout


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def plan():
    diff = exact_diff()
    if not diff or len(diff.encode()) > context.MAX_CLAUDE_EXECUTION_FILE_BYTES:
        raise RuntimeError('invalid_diff_size')
    max_bytes = _limit('CLAUDE_REVIEW_CHUNK_MAX_BYTES', 32000, 128000)
    max_lines = _limit('CLAUDE_REVIEW_CHUNK_MAX_LINES', 600, 2400)
    max_chunks = _limit('CLAUDE_REVIEW_MAX_CHUNKS', 100, 100)
    lines = diff.splitlines(keepends=True)
    units = []
    start, size = 0, 0
    for index, line in enumerate(lines):
        length = len(line.encode('utf-8'))
        if length > max_bytes:
            raise RuntimeError('oversized_diff_line')
        if index > start and (size + length > max_bytes or index - start >= max_lines):
            units.append({'index': len(units) + 1, 'start': start + 1, 'end': index,
                          'sha256': _digest(''.join(lines[start:index]))})
            start, size = index, 0
        size += length
    units.append({'index': len(units) + 1, 'start': start + 1, 'end': len(lines),
                  'sha256': _digest(''.join(lines[start:]))})
    if len(units) > max_chunks:
        raise RuntimeError('chunk_limit_exceeded')
    return {'binding': {**finalize._revision_binding(diff.encode()),
                        'run_id': os.environ['GITHUB_RUN_ID'],
                        'run_attempt': os.environ['GITHUB_RUN_ATTEMPT']},
            'chunks': units, 'diff': diff}


def _start_context(lines):
    """Locate a continuation without duplicating or omitting authoritative lines."""
    result = {}
    left = right = None
    for line in lines:
        if line.startswith('diff --git '):
            result = {'file_header': line.rstrip('\n')}
            left = right = None
        match = re.match(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
        if match:
            result['hunk_header'] = line.rstrip('\n')
            left, right = map(int, match.groups())
        elif left is not None:
            if line.startswith(('-', ' ')):
                left += 1
            if line.startswith(('+', ' ')):
                right += 1
    if left is not None:
        result.update(next_left=left, next_right=right)
    return result


def select(index, digest):
    current = plan()
    if not 1 <= index <= len(current['chunks']):
        raise RuntimeError('invalid_chunk_index')
    unit = current['chunks'][index - 1]
    if unit['sha256'] != digest:
        raise RuntimeError('chunk_revision_mismatch')
    lines = current['diff'].splitlines(keepends=True)
    part = ''.join(lines[unit['start'] - 1:unit['end']])
    (Path.cwd() / '.ci-pr-review.diff').write_text(part, encoding='utf-8')
    return {'index': index, 'total': len(current['chunks']),
            'full_diff_start': unit['start'], 'full_diff_end': unit['end'],
            'context': _start_context(lines[:unit['start'] - 1])}


def extract(execution_file, output_file):
    events = finalize._root_events(context._claude_execution_events(execution_file))
    _, result = finalize._session(events)
    if result.get('subtype') != 'success' or result.get('is_error') is not False:
        raise RuntimeError('chunk_execution_failed')
    path, _, lines = finalize._diff()
    try:
        finalize._require_complete_diff(events, path, lines)
    except finalize.IncompleteDiffRead as error:
        print('Claude chunk diff-read coverage: ' + json.dumps(error.coverage))
        raise
    context._command_extract(execution_file, output_file)


def aggregate(reports):
    current = plan()
    expected = current['chunks']
    if len(reports) != len(expected):
        raise RuntimeError('missing_chunk_results')
    by_index = {}
    for report in reports:
        if not isinstance(report, dict) or report.get('binding') != current['binding']:
            raise RuntimeError('chunk_revision_mismatch')
        unit = report.get('chunk')
        index = unit.get('index') if isinstance(unit, dict) else None
        if not isinstance(index, int) or not 1 <= index <= len(expected) or index in by_index:
            raise RuntimeError('invalid_chunk_results')
        if unit != expected[index - 1] or report.get('status') != 'success':
            raise RuntimeError('incomplete_chunk_review')
        by_index[index] = context._validate_claude_result_contract(report.get('result'))
    findings, verdicts, summaries = {}, {}, []
    for index in range(1, len(expected) + 1):
        result = by_index[index]
        summaries.append(result['summary'].strip())
        for finding in result['findings']:
            key = (finding['path'], finding['side'], finding['line'])
            if key not in findings or finding['severity'] == 'P1':
                findings[key] = finding
        for verdict in result['thread_verdicts']:
            key = verdict['thread_id']
            if key in verdicts and verdicts[key]['verdict'] != verdict['verdict']:
                verdicts[key] = {**verdict, 'verdict': 'needs_human',
                                 'reason': 'Chunk reviews disagree; human review is required.'}
            elif key not in verdicts:
                verdicts[key] = verdict
    ordered = sorted(findings.values(), key=lambda item: (item['severity'], item['path'], item['line']))
    return {'summary': '\n\n'.join(dict.fromkeys(summaries))[:context.MAX_CLAUDE_SUMMARY_CHARACTERS],
            'findings': ordered[:5], 'thread_verdicts': list(verdicts.values())[:200]}


def _outputs(values):
    with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as output:
        for key, value in values.items():
            encoded = value if isinstance(value, str) else json.dumps(value, separators=(',', ':'))
            output.write(f'{key}={encoded}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'select', 'extract', 'record', 'aggregate'))
    parser.add_argument('--index', type=int)
    parser.add_argument('--sha256')
    parser.add_argument('--execution-file', type=Path)
    parser.add_argument('--result', type=Path)
    parser.add_argument('--reports', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'plan':
        _outputs({'matrix': {'include': plan()['chunks']}})
    elif args.command == 'select':
        scope = select(args.index, args.sha256)
        _outputs({'chunk_scope': scope})
        _outputs({'read_plan': finalize.read_plan()})
    elif args.command == 'extract':
        extract(args.execution_file, args.output)
    elif args.command == 'record':
        current = plan()
        if not 1 <= args.index <= len(current['chunks']):
            raise RuntimeError('invalid_chunk_index')
        unit = current['chunks'][args.index - 1]
        if unit['sha256'] != args.sha256 or unit['sha256'] != _digest((Path.cwd() / '.ci-pr-review.diff').read_text()):
            raise RuntimeError('chunk_revision_mismatch')
        result_path = context._validate_cli_path(args.result, 'result file')
        if result_path.stat().st_size > context.MAX_CLAUDE_REVIEW_RESULT_BYTES:
            raise RuntimeError('invalid_result_size')
        result = context._validate_claude_result_contract(json.loads(result_path.read_text()))
        execution = {key: safe_label(value) for key, value in os.environ.items()
                     if key.startswith('CLAUDE_REVIEW_') and key.endswith(('_OUTCOME', '_VALIDATION'))}
        for suffix in ('PROVIDER', 'PRIMARY_PROVIDER', 'FALLBACK_PROVIDER', 'PRIMARY_MODEL', 'FALLBACK_MODEL'):
            key = 'CLAUDE_REVIEW_' + suffix
            execution[key] = safe_label(os.environ.get(key, 'unknown'))
        endpoint = os.environ.get('CLAUDE_REVIEW_ENDPOINT', '')
        execution['CLAUDE_REVIEW_ENDPOINT'] = endpoint if endpoint in PUBLIC_ENDPOINTS else ''
        report = {'binding': current['binding'], 'chunk': unit, 'status': 'success', 'result': result,
                  'provider': safe_label(os.environ['CLAUDE_REVIEW_PROVIDER']),
                  'model': safe_label(os.environ['CLAUDE_REVIEW_MODEL']), 'execution': execution}
        context._validate_cli_path(args.output, 'report file').write_text(json.dumps(report), encoding='utf-8')
    else:
        directory = context._validate_cli_path(args.reports, 'reports directory')
        reports = []
        for path in directory.rglob('chunk-result.json'):
            if path.stat().st_size > context.MAX_CLAUDE_REVIEW_RESULT_BYTES:
                raise RuntimeError('invalid_result_size')
            reports.append(json.loads(path.read_text()))
        result = aggregate(reports)
        context._validate_cli_path(args.output, 'result file').write_text(json.dumps(result), encoding='utf-8')
        ordered = sorted(reports, key=lambda report: report['chunk']['index'])
        _outputs({'chunk_executions': [report['execution'] for report in ordered],
                  'models': ', '.join(sorted({report['model'] for report in ordered}))})
        print(f'Validated complete SDK coverage: {len(reports)}/{len(reports)} chunks.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError):
        raise SystemExit('Claude chunk review is incomplete or invalid; no completed review was produced.') from None

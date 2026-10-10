#!/usr/bin/env python3
"""Allow one tool-free completion of a fully read, turn-limited Claude review."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import pr_review_context as context


def _root_events(events):
    return [event for event in events if isinstance(event, dict) and not event.get('parent_tool_use_id')]


def _session(events):
    """Accept only one initialized SDK session with a matching terminal result."""
    initial = [event for event in events if event.get('type') == 'system' and event.get('subtype') == 'init']
    results = [event for event in events if event.get('type') == 'result']
    if len(initial) != 1 or len(results) != 1 or events[-1] is not results[0]:
        raise RuntimeError('invalid_session_events')
    session = initial[0].get('session_id')
    if not isinstance(session, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', session):
        raise RuntimeError('invalid_session_id')
    if any(event.get('session_id', session) != session for event in events):
        raise RuntimeError('mixed_sessions')
    return session, results[0]


def _diff():
    path = (Path.cwd() / '.ci-pr-review.diff').resolve()
    if not 0 < path.stat().st_size <= context.MAX_CLAUDE_EXECUTION_FILE_BYTES:
        raise RuntimeError('invalid_diff_size')
    raw = path.read_bytes()
    return path, raw, raw.decode('utf-8').splitlines()


def _returned_lines(block):
    if block.get('is_error') not in (None, False):
        return []
    content = block.get('content')
    if isinstance(content, list):
        content = '\n'.join(item['text'] for item in content if isinstance(item, dict)
                            and item.get('type') == 'text' and isinstance(item.get('text'), str))
    if not isinstance(content, str):
        return []
    return re.findall(r'^[ \t]*([1-9]\d*) *[→\t:](.*)$', content, re.MULTILINE)


def _completed_lines(event, pending, lines):
    seen = set()
    for block in context._sdk_content(event, 'user'):
        call = block.get('tool_use_id')
        if block.get('type') != 'tool_result' or not isinstance(call, str) or call not in pending:
            continue
        pending.remove(call)
        for number, text in _returned_lines(block):
            index = int(number) - 1
            if 0 <= index < len(lines) and text == lines[index]:
                seen.add(index)
    return seen


def _require_complete_diff(events, path, lines):
    """Match every numbered returned line to the current diff, including blank lines."""
    pending = set()
    seen = set()
    for event in events:
        if event.get('type') == 'result':
            break
        if event.get('type') == 'system' and event.get('subtype') == 'init':
            pending.clear()
            seen.clear()
        context._track_diff_calls(event, pending, path)
        seen.update(_completed_lines(event, pending, lines))
    if not lines or len(seen) != len(lines):
        raise RuntimeError('diff_incomplete')


def _revision_binding(raw):
    return {'base_sha': context._required_commit_sha('BASE_SHA'),
            'head_sha': context._required_commit_sha('HEAD_SHA'),
            'diff_sha256': hashlib.sha256(raw).hexdigest()}


def _write_proof(path, proof):
    """Persist metadata only; SDK tool output and credentials never enter the proof."""
    path = context._validate_cli_path(path, 'proof file')
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(proof, stream)
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def prepare(execution_file, proof_file):
    """Return safe action outputs. All other failures go directly to fallback."""
    try:
        events = _root_events(context._claude_execution_events(execution_file))
        session, result = _session(events)
        if result.get('subtype') != 'error_max_turns' or result.get('is_error') is not True:
            return {'ready': 'false', 'reason': 'not_turn_limit'}
        path, raw, lines = _diff()
        _require_complete_diff(events, path, lines)
        _write_proof(proof_file, {**_revision_binding(raw), 'session_id': session})
    except (OSError, ValueError, RuntimeError):
        return {'ready': 'false', 'reason': 'missing_or_incomplete_evidence'}
    return {'ready': 'true', 'reason': 'turn_limit_after_complete_diff_read', 'session_id': session}


def extract(execution_file, proof_file, output_file):
    proof_path = context._validate_cli_path(proof_file, 'proof file')
    if proof_path.stat().st_size > 4096:
        raise RuntimeError('invalid_finalization_proof')
    proof = json.loads(proof_path.read_text())
    _, raw, _ = _diff()
    binding = _revision_binding(raw)
    if not isinstance(proof, dict) or any(proof.get(key) != value for key, value in binding.items()):
        raise RuntimeError('finalization_revision_mismatch')
    raw_events = context._claude_execution_events(execution_file)
    if any(isinstance(event, dict) and event.get('parent_tool_use_id') for event in raw_events):
        raise RuntimeError('finalization_used_subagent')
    events = _root_events(raw_events)
    session, result = _session(events)
    if session != proof.get('session_id'):
        raise RuntimeError('finalization_session_mismatch')
    if result.get('subtype') != 'success' or result.get('is_error') is not False:
        raise RuntimeError('finalization_failed')
    if any(block.get('type') == 'tool_use' for event in events for block in context._sdk_content(event, 'assistant')):
        raise RuntimeError('finalization_used_tools')
    text = context._result_event_text(result)
    if not text:
        raise RuntimeError('finalization_missing_result')
    normalized = context._normalized_claude_result(text, binding['base_sha'], binding['head_sha'])
    output = context._validate_cli_path(output_file, 'output file')
    output.write_text(normalized + '\n', encoding='utf-8')
    print('Validated the resumed Claude review against its session, revisions and complete diff-read evidence.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'extract'))
    parser.add_argument('--execution-file', required=True, type=Path)
    parser.add_argument('--proof-file', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'prepare':
        values = prepare(args.execution_file, args.proof_file)
        print('Claude finalization: ' + values['reason'])
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
            output.writelines(f'{key}={value}\n' for key, value in values.items())
    else:
        if args.output is None:
            parser.error('--output is required for extract')
        extract(args.execution_file, args.proof_file, args.output)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError):
        raise SystemExit('Claude finalization failed validation; no review result was produced.') from None

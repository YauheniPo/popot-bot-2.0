#!/usr/bin/env python3
"""Retire exact recorded managed edits before Git merges the operator's changes.

The complete tracked working tree is retained in a recovery ref before any write.
Unknown or locally modified patch blocks remain untouched and use normal Git merge.
"""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import subprocess
import time


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], timeout=60)


def retire(original, current, records):
    spans = []
    for marker, record in records.items():
        if not isinstance(record, dict):
            continue
        block = record.get('source', '')
        if (not isinstance(block, str) or not marker.startswith('# Local Hermes:')
                or marker not in block or current.count(block) != 1
                or hashlib.sha256(block.encode()).hexdigest() != record.get('digest')):
            continue
        start = current.index(block)
        end = start + len(block)
        # difflib can attach an adjacent blank-line separator to an insertion.
        # Admit only newline bytes outside the exact recorded block.
        while start and current[start - 1] in '\r\n':
            start -= 1
        while end < len(current) and current[end] in '\r\n':
            end += 1
        spans.append((start, end))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    spans = merged
    if not spans:
        return current
    before, after = original.splitlines(keepends=True), current.splitlines(keepends=True)
    offsets = [0]
    for line in after:
        offsets.append(offsets[-1] + len(line))
    edits = []
    for tag, i, end_i, j, end_j in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        old, new = ''.join(before[i:end_i]), ''.join(after[j:end_j])
        # Character-level refinement retains operator edits sharing a line with
        # a managed block; only exact recorded bytes may be retired.
        for change, a, b, c, d in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if change == 'equal':
                continue
            start, end = offsets[j] + c, offsets[j] + d
            if any(lo <= start < end <= hi or lo < start == end < hi for lo, hi in spans):
                edits.append((start, end, old[a:b]))
    for start, end, replacement in reversed(edits):
        current = current[:start] + replacement + current[end:]
    return current


def prepare(root):
    state = root / '.local-hermes-patches.json'
    if not state.is_file():
        return None
    if git(root, 'diff', '--cached', '--name-only'):
        raise RuntimeError('Installed checkout has staged changes; reconcile them before upgrading')
    records = json.loads(state.read_text())
    if not isinstance(records, dict):
        raise RuntimeError('Invalid managed patch state')
    writes = []
    for name in git(root, 'diff', 'HEAD', '--name-only', '-z').split(b'\0'):
        if not name:
            continue
        relative = name.decode()
        target = root / relative
        if target.is_symlink() or not target.is_file():
            continue
        try:
            original = git(root, 'show', f'HEAD:{relative}').decode()
            current = target.read_text()
        except UnicodeError:
            continue
        cleaned = retire(original, current, records)
        if cleaned != current:
            writes.append((target, cleaned))
    if not writes:
        return None
    snapshot = git(root, 'stash', 'create', 'managed patch upgrade recovery').decode().strip()
    if not snapshot:
        raise RuntimeError('Cannot preserve the pre-upgrade working tree')
    reference = f'refs/hermes-managed/pre-upgrade/{time.time_ns()}'
    git(root, 'update-ref', reference, snapshot, '0' * 40)
    for target, cleaned in writes:
        target.write_text(cleaned)
    return reference


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkout', type=Path)
    args = parser.parse_args()
    recovery = prepare(args.checkout)
    print(f'Managed patches retired; recovery: {recovery}' if recovery else 'No recorded patches to retire')

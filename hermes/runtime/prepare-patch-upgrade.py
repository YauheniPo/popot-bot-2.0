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
    # Shell-free: dynamic values are git paths/refs consumed after fixed subcommands or -C.
    return subprocess.check_output(['git', '-C', str(root), *args], timeout=60)  # NOSONAR


def recorded_span(current, marker, record):
    """Return the newline-expanded span of one exact recorded block, else None."""
    if not isinstance(record, dict):
        return None
    block = record.get('source', '')
    if (not isinstance(block, str) or not marker.startswith('# Local Hermes:')
            or marker not in block or current.count(block) != 1
            or hashlib.sha256(block.encode()).hexdigest() != record.get('digest')):
        return None
    start = current.index(block)
    end = start + len(block)
    # difflib can attach an adjacent blank-line separator to an insertion.
    # Admit only newline bytes outside the exact recorded block.
    while start and current[start - 1] in '\r\n':
        start -= 1
    while end < len(current) and current[end] in '\r\n':
        end += 1
    return start, end


def merged_spans(current, records):
    """Return recorded block spans with overlapping ones merged together."""
    spans = [span for span in (recorded_span(current, marker, record)
                               for marker, record in records.items())
             if span is not None]
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def char_edits(old, new, offset, spans):
    """Keep only the changed bytes that fall inside a retired span."""
    edits = []
    for change, a, b, c, d in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if change == 'equal':
            continue
        start, end = offset + c, offset + d
        if any(lo <= start < end <= hi or lo < start == end < hi for lo, hi in spans):
            edits.append((start, end, old[a:b]))
    return edits


def block_edits(before, after, offsets, spans):
    """Refine each changed block so operator edits sharing its line survive."""
    edits = []
    for tag, i, end_i, j, end_j in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        # Character-level refinement retains operator edits sharing a line with
        # a managed block; only exact recorded bytes may be retired.
        edits.extend(char_edits(''.join(before[i:end_i]), ''.join(after[j:end_j]), offsets[j], spans))
    return edits


def retire(original, current, records):
    spans = merged_spans(current, records)
    if not spans:
        return current
    before, after = original.splitlines(keepends=True), current.splitlines(keepends=True)
    offsets = [0]
    for line in after:
        offsets.append(offsets[-1] + len(line))
    parts, cursor = [], 0
    for start, end, replacement in block_edits(before, after, offsets, spans):
        parts.extend((current[cursor:start], replacement))
        cursor = end
    parts.append(current[cursor:])
    return ''.join(parts)


def pending_writes(root, names, records):
    """Return the (target, cleaned) pairs whose recorded blocks can be retired."""
    writes = []
    for name in names:
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
    return writes


def prepare(root):
    state = root / '.local-hermes-patches.json'
    if not state.is_file():
        return None
    if git(root, 'diff', '--cached', '--name-only'):
        raise RuntimeError('Installed checkout has staged changes; reconcile them before upgrading')
    records = json.loads(state.read_text())
    if not isinstance(records, dict):
        raise RuntimeError('Invalid managed patch state')
    names = git(root, 'diff', 'HEAD', '--name-only', '-z').split(b'\0')
    writes = pending_writes(root, names, records)
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

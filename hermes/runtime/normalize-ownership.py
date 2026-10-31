#!/usr/bin/env python3
"""Repair ownership in one native traversal, without following symlinks."""

import argparse
import os
import re
import subprocess

# User and group operands are embedded in the find expression and in the
# chown owner spec below. Restrict them to ordinary POSIX account names
# (or numeric ids) before any child process runs, so agent-supplied values
# such as '-exec', '--help' or 'a:b' are rejected instead of reaching
# find/chown (S8705).
OWNER_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]*\$?$')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root')
    parser.add_argument('user')
    parser.add_argument('group')
    args = parser.parse_args()
    if not OWNER_RE.fullmatch(args.user):
        parser.error(f'invalid user operand: {args.user!r}')
    if not OWNER_RE.fullmatch(args.group):
        parser.error(f'invalid group operand: {args.group!r}')
    result = subprocess.run([
        'find', '-P', os.path.abspath(args.root),
        '(', '!', '-user', args.user, '-o', '!', '-group', args.group, ')',
        '-exec', 'chown', '-h', f'{args.user}:{args.group}', '{}', '+', '-print',
    ], stdout=subprocess.PIPE, check=True)
    print('ownership-changed=' + str(bool(result.stdout)).lower())


if __name__ == '__main__':
    main()

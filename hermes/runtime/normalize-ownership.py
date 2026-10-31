#!/usr/bin/env python3
"""Repair ownership in one traversal, without following symlinks."""

import argparse
import grp
import os
import pwd
import re
import sys

# User and group operands select the target owner and never reach an
# external command: names are validated against the POSIX account shape
# and resolved to numeric ids in-process, then the traversal repairs
# mismatched entries with os.chown instead of find/chown (S8705).
OWNER_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]*\$?$')


def owner_id(value, database):
    if value.isdigit():
        return int(value)
    return database(value)


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
    try:
        uid = owner_id(args.user, lambda name: pwd.getpwnam(name).pw_uid)
        gid = owner_id(args.group, lambda name: grp.getgrnam(name).gr_gid)
    except KeyError as error:
        parser.error(f'unknown owner operand: {error.args[0]!r}')

    root = os.path.abspath(args.root)
    errors = []
    changed = []

    def repair(path):
        try:
            status = os.lstat(path)
        except OSError as error:
            errors.append(f'{path}: {error}')
            return
        if status.st_uid == uid and status.st_gid == gid:
            return
        try:
            os.chown(path, uid, gid, follow_symlinks=False)
        except (OSError, OverflowError) as error:
            errors.append(f'{path}: {error}')
            return
        changed.append(path)

    repair(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False,
                                                onerror=lambda error: errors.append(str(error))):
        for name in (*filenames, *dirnames):
            repair(os.path.join(dirpath, name))

    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        raise SystemExit(1)
    for path in changed:
        print(path)
    print('ownership-changed=' + str(bool(changed)).lower())


if __name__ == '__main__':
    main()

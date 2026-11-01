#!/usr/bin/env python3
"""Repair ownership in one traversal, without following symlinks."""

import argparse
import grp
import os
import pwd
import re
import sys

# Operands select the target owner and never reach an external command.
# User and group names are validated against the POSIX account shape and
# resolved to numeric ids in-process (S8705). The root path is canonicalized
# with realpath and must resolve to an existing directory before any repair,
# so parent-directory symlinks or path components cannot redirect the
# traversal (S8707). Repair itself stays inside the interpreter: os.lchown
# fixes mismatched entries without following symlinks.
OWNER_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]*\$?$')


def owner_id(value, database):
    if value.isdigit():
        return int(value)
    return database(value)


def resolve_root(path):
    """Return the canonical root path, requiring an existing directory."""
    root = os.path.realpath(path)
    if not os.path.isdir(root):
        raise ValueError(f'root must resolve to an existing directory: {path!r}')
    return root


def repair(path, uid, gid, errors):
    try:
        status = os.lstat(path)
    except OSError as error:
        errors.append(f'{path}: {error}')
        return False
    if status.st_uid == uid and status.st_gid == gid:
        return False
    try:
        os.lchown(path, uid, gid)
    except (OSError, OverflowError) as error:
        errors.append(f'{path}: {error}')
        return False
    return True


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
    try:
        root = resolve_root(args.root)
    except ValueError as error:
        parser.error(str(error))

    errors = []
    changed = repair(root, uid, gid, errors)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False,
                                                onerror=lambda error: errors.append(str(error))):
        for name in (*filenames, *dirnames):
            changed = repair(os.path.join(dirpath, name), uid, gid, errors) or changed

    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        raise SystemExit(1)
    print('ownership-changed=' + str(changed).lower())


if __name__ == '__main__':
    main()

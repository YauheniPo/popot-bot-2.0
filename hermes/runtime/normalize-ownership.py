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
# resolved to numeric ids in-process (S8705). The root must be an absolute
# path built from POSIX-shaped components, so no caller-supplied operand can
# smuggle traversal, whitespace, or control characters into the chown sink;
# dot-prefixed components such as .hermes are allowed, while the exact "."
# and ".." entries are rejected by the per-component lookaheads. The path is
# then canonicalized with realpath and must resolve to an existing directory
# before any repair, so parent-directory symlinks or path components cannot
# redirect the traversal (S8707). Repair itself stays inside the
# interpreter: os.lchown fixes mismatched entries without following symlinks.
OWNER_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]*\$?$')
ROOT_RE = re.compile(r'^/(?:(?!\.(?:/|$))(?!\.\.(?:/|$))[A-Za-z0-9_.-]+/)*(?!\.(?:/|$))(?!\.\.(?:/|$))[A-Za-z0-9_.-]+/?$')


def owner_id(value, database):
    if value.isdigit():
        return int(value)
    return database(value)


def resolve_root(path):
    """Return the root path, requiring a real directory without symlinks."""
    root = os.path.normpath(path.rstrip('/'))
    if os.path.realpath(root) != root:
        raise ValueError(f'root must not contain symlinks: {path!r}')
    if not os.path.isdir(root):
        raise ValueError(f'root must resolve to an existing directory: {path!r}')
    return root


def repair(dir_fd, name, uid, gid, errors):
    """Repair one entry relative to dir_fd without following a final symlink."""
    label = name if name == '.' else os.path.join('.', name)
    try:
        status = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as error:
        errors.append(f'{label}: {error}')
        return False
    if status.st_uid == uid and status.st_gid == gid:
        return False
    try:
        os.chown(name, uid, gid, dir_fd=dir_fd, follow_symlinks=False)
    except (OSError, OverflowError) as error:
        errors.append(f'{label}: {error}')
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
    if not ROOT_RE.fullmatch(args.root):
        parser.error(f'invalid root operand: {args.root!r}')
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
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        parser.error(f'cannot open root without following symlinks: {error}')
    try:
        # The traversal is anchored on a directory descriptor opened with
        # O_NOFOLLOW, so swapping the root path after validation cannot
        # redirect chown outside the validated tree.
        changed = repair(root_fd, '.', uid, gid, errors)
        for _, dirnames, filenames, dir_fd in os.fwalk(
                '.', dir_fd=root_fd, follow_symlinks=False,
                onerror=lambda error: errors.append(str(error))):
            for name in (*filenames, *dirnames):
                changed = repair(dir_fd, name, uid, gid, errors) or changed
    finally:
        os.close(root_fd)

    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        raise SystemExit(1)
    print('ownership-changed=' + str(changed).lower())


if __name__ == '__main__':
    main()

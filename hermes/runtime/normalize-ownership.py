#!/usr/bin/env python3
"""Repair ownership in one native traversal, without following symlinks."""

import argparse
import os
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root')
    parser.add_argument('user')
    parser.add_argument('group')
    args = parser.parse_args()
    result = subprocess.run([
        'find', '-P', os.path.abspath(args.root),
        '(', '!', '-user', args.user, '-o', '!', '-group', args.group, ')',
        '-exec', 'chown', '-h', f'{args.user}:{args.group}', '{}', '+', '-print',
    ], stdout=subprocess.PIPE, check=True)
    print('ownership-changed=' + str(bool(result.stdout)).lower())


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Publish the selected PM Node/npm at the existing private global-package prefix."""

import argparse
import os
from pathlib import Path
import shlex
import tempfile


def publish(home):
    from pm import installed_package
    node, npm = installed_package('node'), installed_package('npm')
    if node is None or node.binary is None or npm is None:
        raise RuntimeError('PM Node/npm is unavailable; finish the managed installation')
    commands = {'node': [str(node.binary)]}
    for name in ('npm', 'npx'):
        cli = npm.path / f'lib/node_modules/npm/bin/{name}-cli.js'
        if not cli.is_file():
            raise RuntimeError(f'PM {name} entry is missing')
        commands[name] = [str(node.binary), str(cli)]
    prefix = home / 'node'
    directory = prefix / 'bin'
    if prefix.is_symlink() or directory.is_symlink():
        raise RuntimeError('Managed Node prefix must not be a symlink')
    directory.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, command in commands.items():
        content = '#!/bin/sh\n'
        if name != 'node':
            content += ('if [ -z "${NPM_CONFIG_PREFIX:-}" ]; then\n'
                        f'    export NPM_CONFIG_PREFIX={shlex.quote(str(prefix))}\nfi\n')
        content += 'exec ' + shlex.join(command) + ' "$@"\n'
        target = directory / name
        if not target.is_symlink() and target.is_file() and target.read_bytes() == content.encode():
            if target.stat().st_mode & 0o777 != 0o750:
                target.chmod(0o750)
                changed = True
            continue
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(content.encode())
                stream.flush()
                temporary.chmod(0o750)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        changed = True
    return changed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-home', type=Path, required=True)
    args = parser.parse_args()
    print('changed' if publish(args.hermes_home) else 'unchanged')

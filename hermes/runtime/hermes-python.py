#!/usr/bin/env python3
"""Run managed helpers in the active Hermes runtime, including pre-PM backups.

Installed as HERMES_HOME/python. PM owns dependency selection and leases; never
persist a generation's interpreter path or silently fall back from a broken PM.
"""

import os
from pathlib import Path
import runpy
import sys


def run_python(args):
    """The Python entry modes used by managed scripts, without shell interpolation."""
    if not args:
        raise SystemExit('Usage: python SCRIPT | -c CODE | -m MODULE | - [arguments]')
    mode, *tail = args
    if mode in ('-c', '-m'):
        if not tail:
            raise SystemExit(f'{mode} requires an argument')
        value, *tail = tail
        sys.argv = ['-c' if mode == '-c' else value, *tail]
        if mode == '-m':
            runpy.run_module(value, run_name='__main__', alter_sys=True)
            return
        code, filename = value, '<string>'
    elif mode == '-':
        sys.argv = ['-', *tail]
        code, filename = sys.stdin.read(), '<stdin>'
    elif mode in ('-V', '--version'):
        print(f'Python {sys.version.split()[0]}')
        return
    else:
        sys.argv = [mode, *tail]
        sys.path.insert(0, str(Path(mode).resolve().parent))
        runpy.run_path(mode, run_name='__main__')
        return
    exec(compile(code, filename, 'exec'), {'__name__': '__main__', '__file__': filename})


def main():
    home = Path(__file__).resolve().parent
    root = Path(os.environ.get('HERMES_INSTALL_DIR', home / 'hermes-agent'))
    if not (root / 'pm/environments.py').is_file():
        legacy = root / 'venv/bin/python'
        if not legacy.is_file():
            raise RuntimeError('Hermes runtime is missing')
        os.execv(str(legacy), [str(legacy), *sys.argv[1:]])
    # These upstream modules use only stdlib and support the host Python 3.11.
    os.environ.setdefault('HERMES_HOME', str(home))
    sys.path.insert(0, str(root))
    from hermes_cli._launchers import resolve_store_python, runtime_command
    python = resolve_store_python(root)
    if python is None:
        raise RuntimeError('Hermes PM runtime is missing; finish the managed installation')
    code = f"runpy.run_path({str(Path(__file__).resolve())!r})['run_python'](sys.argv[1:])"
    command = runtime_command(root, sys.argv[1:], code=code, python=python, home=home)
    os.execv(command[0], command)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        raise SystemExit(1)

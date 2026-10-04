#!/usr/bin/env python3
"""Update Agent-Reach and its dependencies in an isolated user environment."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


SOURCE = 'https://github.com/Panniantong/Agent-Reach/archive/{}.zip'
METADATA = "from importlib.metadata import distribution; print(distribution('agent-reach').read_text('direct_url.json') or '{}')"
VERSIONS = "import json; from importlib.metadata import distributions; print(json.dumps({d.metadata['Name']: d.version for d in distributions()}))"


def run(args, timeout=60):
    result = subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout,
                            env={**os.environ, 'UV_HTTP_TIMEOUT': '30', 'UV_HTTP_RETRIES': '2'})
    return result.stdout.strip()


def installed_from(python, url):
    if not python.exists():
        return False
    try:
        return json.loads(run([str(python), '-c', METADATA])).get('url') == url
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False


def install(home, revision, uv):
    if revision == 'latest':
        print('Resolving latest Agent-Reach source', flush=True)
        revision = run([sys.executable, str(Path(__file__).with_name('resolve-tool-version.py')),
                        '--package', 'agent-reach', '--requested', revision], timeout=60)
    # Validate revision format immediately before any URL construction
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Agent-Reach revision must be a full commit SHA')
    venv = home / '.local/share/hermes-tools/agent-reach'
    python = venv / 'bin/python'
    launchers = {home / '.local/bin' / name: venv / 'bin' / name
                 for name in ('agent-reach', 'yt-dlp')}
    # Never overwrite an owner's existing tool or an unrelated symlink.
    for link, target in launchers.items():
        if link.is_symlink():
            if link.readlink() != target:
                raise RuntimeError(f'unmanaged launcher already exists: {link}')
        elif link.exists():
            raise RuntimeError(f'unmanaged launcher already exists: {link}')
    url = SOURCE.format(revision)
    changed = False
    if not python.exists():
        venv.parent.mkdir(parents=True, exist_ok=True)
        print('Creating isolated Agent-Reach environment', flush=True)
        run([uv, 'venv', '--python', sys.executable, '--no-python-downloads', str(venv)])
        changed = True
    source_changed = not installed_from(python, url)
    before = installed_versions(python)
    repair = any(not target.exists() for target in launchers.values())
    print(f'Checking Agent-Reach revision {revision} and latest dependencies', flush=True)
    args = [uv, 'pip', 'install', '--python', str(python), '--upgrade']
    if source_changed or repair:
        args += ['--reinstall-package', 'agent-reach', '--reinstall-package', 'yt-dlp']
    run([*args, url], timeout=600)
    if not installed_from(python, url):
        raise RuntimeError('Agent-Reach installed source does not match the requested revision')
    changed = changed or source_changed or repair or before != installed_versions(python)
    # Version probes do not contact platforms or import browser credentials.
    for target in launchers.values():
        run([str(target), 'version' if target.name == 'agent-reach' else '--version'])
    for link, target in launchers.items():
        if not link.is_symlink():
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
            changed = True
    return changed


def installed_versions(python):
    return json.loads(run([str(python), '-c', VERSIONS]))


def resolve_uv(hermes_home):
    # The pinned Hermes installer owns uv here; it need not be on PATH.
    managed_uv = hermes_home / 'bin/uv'
    if managed_uv.is_file() and os.access(managed_uv, os.X_OK):
        return str(managed_uv)
    return shutil.which('uv')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--settings', type=Path)
    source.add_argument('--revision')
    args = parser.parse_args()
    revision = args.revision
    if args.settings:
        import yaml
        settings = yaml.safe_load(args.settings.read_text(encoding='utf-8'))
        revision = settings['vps_tools']['agent_reach']['revision']
    hermes_home = Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes')
    uv = resolve_uv(hermes_home)
    if not uv:
        parser.error(f'uv is required at {hermes_home / "bin/uv"} or on PATH')
    try:
        changed = install(Path.home(), revision, uv)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # Do not dump installer output which may contain environment credentials.
        print(f'Agent-Reach installation failed: {exc}', file=sys.stderr)
        return 1
    print(f'{int(changed)} change(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())

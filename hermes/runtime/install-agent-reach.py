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
SHA_PATTERN = r'[0-9a-f]{40}'


def run(args, timeout=60):
    result = subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout,
                            env={**os.environ, 'UV_HTTP_TIMEOUT': '30', 'UV_HTTP_RETRIES': '2'})
    return result.stdout.strip()


def _validate_revision(revision):
    """Validate revision format. Returns True if 'latest', validates SHA otherwise."""
    if revision == 'latest':
        return True
    return re.fullmatch(SHA_PATTERN, revision) is not None


def installed_from(python, url):
    if not python.exists():
        return False
    try:
        return json.loads(run([str(python), '-c', METADATA])).get('url') == url
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False


def _resolve_latest_revision():
    """Resolve 'latest' to a concrete SHA using resolve-tool-version.py."""
    print('Resolving latest Agent-Reach source', flush=True)
    return run([sys.executable, str(Path(__file__).with_name('resolve-tool-version.py')),
                '--package', 'agent-reach', '--requested', 'latest'], timeout=60)


def _validate_and_prepare(home, revision):
    """Validate revision and prepare paths. Returns (venv, python, launchers, url)."""
    if not _validate_revision(revision):
        raise ValueError('Agent-Reach revision must be a full commit SHA or "latest"')
    
    if revision == 'latest':
        revision = _resolve_latest_revision()
    
    if not re.fullmatch(SHA_PATTERN, revision):
        raise ValueError('Agent-Reach revision must be a full commit SHA')

    venv = home / '.local/share/hermes-tools/agent-reach'
    python = venv / 'bin/python'
    launchers = {home / '.local/bin' / name: venv / 'bin' / name
                 for name in ('agent-reach', 'yt-dlp')}
    return venv, python, launchers, SOURCE.format(revision)


def _check_launchers(launchers):
    """Check launchers for conflicts. Raises RuntimeError if unmanaged launcher exists."""
    for link, target in launchers.items():
        if link.is_symlink():
            if link.readlink() != target:
                raise RuntimeError(f'unmanaged launcher already exists: {link}')
        elif link.exists():
            raise RuntimeError(f'unmanaged launcher already exists: {link}')


def _create_venv_if_needed(python, uv, venv):
    """Create venv if it doesn't exist. Returns True if created."""
    if not python.exists():
        venv.parent.mkdir(parents=True, exist_ok=True)
        print('Creating isolated Agent-Reach environment', flush=True)
        run([uv, 'venv', '--python', sys.executable, '--no-python-downloads', str(venv)])
        return True
    return False


def _install_packages(python, uv, url, source_changed, repair):
    """Install or upgrade packages. Returns command args used."""
    args = [uv, 'pip', 'install', '--python', str(python), '--upgrade']
    if source_changed or repair:
        args += ['--reinstall-package', 'agent-reach', '--reinstall-package', 'yt-dlp']
    run([*args, url], timeout=600)


def _verify_installation(python, url):
    """Verify installed source matches requested revision."""
    if not installed_from(python, url):
        raise RuntimeError('Agent-Reach installed source does not match the requested revision')


def _run_version_probes(launchers):
    """Run version probes for installed tools."""
    for target in launchers.values():
        run([str(target), 'version' if target.name == 'agent-reach' else '--version'])


def _create_launchers(launchers):
    """Create launcher symlinks. Returns True if any created."""
    changed = False
    for link, target in launchers.items():
        if not link.is_symlink():
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
            changed = True
    return changed


def install(home, revision, uv):
    venv, python, launchers, url = _validate_and_prepare(home, revision)
    _check_launchers(launchers)
    
    changed = _create_venv_if_needed(python, uv, venv)
    source_changed = not installed_from(python, url)
    before = installed_versions(python)
    repair = any(not target.exists() for target in launchers.values())
    
    print(f'Checking Agent-Reach revision {revision} and latest dependencies', flush=True)
    _install_packages(python, uv, url, source_changed, repair)
    _verify_installation(python, url)
    
    changed = changed or source_changed or repair or before != installed_versions(python)
    _run_version_probes(launchers)
    changed = changed or _create_launchers(launchers)
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
        settings_path = args.settings
        # Validate settings path: must be within config root and have safe extension
        config_root = Path(__file__).resolve().parents[1] / 'config'
        try:
            # Resolve symlinks and verify path stays within config_root
            resolved_path = settings_path.resolve()
            resolved_path.relative_to(config_root.resolve())
            # Ensure the resolved path is a regular file, not a symlink outside config_root
            if not resolved_path.is_file():
                parser.error(f'--settings must be a regular file under {config_root}')
        except ValueError:
            parser.error(f'--settings path must be under {config_root}')
        if settings_path.suffix not in ('.yml', '.yaml'):
            parser.error('--settings must be a .yml or .yaml file')
        import yaml
        settings = yaml.safe_load(settings_path.read_text(encoding='utf-8'))
        # Validate settings structure and revision key presence/type
        vps_tools = settings.get('vps_tools')
        if not isinstance(vps_tools, dict):
            raise ValueError('settings: vps_tools must be a mapping')
        agent_reach = vps_tools.get('agent_reach')
        if not isinstance(agent_reach, dict):
            raise ValueError('settings: vps_tools.agent_reach must be a mapping')
        revision = agent_reach.get('revision')
        if not isinstance(revision, str):
            raise ValueError('settings: vps_tools.agent_reach.revision must be a string')
        # Re-validate revision after YAML load
        if revision != 'latest' and not re.fullmatch(SHA_PATTERN, revision):
            raise ValueError('Agent-Reach revision from settings must be a full commit SHA or "latest"')
    hermes_home = Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes')
    uv = resolve_uv(hermes_home)
    if not uv:
        parser.error(f'uv is required at {hermes_home / "bin/uv"} or on PATH')
    try:
        changed = install(Path.home(), revision, uv)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # Do not dump installer output which may contain environment credentials.
        print('Agent-Reach installation failed', file=sys.stderr)
        return 1
    print(f'{int(changed)} change(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())

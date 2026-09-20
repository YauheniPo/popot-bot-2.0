#!/usr/bin/env python3
"""Provision managed Swarm profiles without resetting work or starting a gateway."""
import argparse
from copy import deepcopy
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
from hermes_config_io import load_config, validated_config_path, write_config

MARKER = '# Managed Hermes Swarm'
SOUL_BEGIN = '<!-- BEGIN ANSIBLE MANAGED SWARM ROLE -->'
SOUL_END = '<!-- END ANSIBLE MANAGED SWARM ROLE -->'
RULES = '''Work only on explicitly dispatched tasks. Never run autonomous loops.
No commit, push, merge, deploy, service restart, destructive operation or external
message without explicit owner approval. Do not read credentials into context.
Do not change model/provider or approvals to bypass a failure.
Researcher and Reviewer inspect and report; only Builder edits task files.
Each worker owns its separate worktree. Do not modify another worker's files.
Reviewer reviews the Builder diff explicitly when assigned; its own worktree
does not automatically contain Builder changes. Report exact commands/results.
Use the user's language for explanations; distinguish verified evidence from
assumptions. Ask about consequential ambiguity; never invent successful checks.
Do not assume shared memory or the main agent's SOUL is loaded in this profile.
Use the dispatched context and applicable repository instructions; request
missing facts instead of silently borrowing another profile's private memory.
Do not write shared MEMORY.md or USER.md. Include durable memory candidates
(fact, source, verification date, scope) in RESULT for the main agent to verify.
Task status and raw logs belong in task artifacts, not permanent memory.
At task completion respond with these six lines (use none when not applicable):
STATE: DONE or BLOCKED or NEEDS_INPUT
FILES_CHANGED: ...
COMMANDS_RUN: ...
RESULT: ...
BLOCKER: ...
NEXT_ACTION: ...
'''


def save_text(path, content, mode=0o600):
    if path.is_symlink():
        raise ValueError('Managed Swarm file must not be a symlink')
    if path.exists() and path.read_text() == content:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            temporary.chmod(mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return 1


def role_soul(previous, instructions):
    block = f'{SOUL_BEGIN}\n{instructions.rstrip()}\n{SOUL_END}'
    if SOUL_BEGIN in previous or SOUL_END in previous:
        if previous.count(SOUL_BEGIN) != 1 or previous.count(SOUL_END) != 1:
            raise ValueError('Malformed managed Swarm role block')
        start, end = previous.index(SOUL_BEGIN), previous.index(SOUL_END)
        if end < start:
            raise ValueError('Malformed managed Swarm role block')
        return previous[:start] + block + previous[end + len(SOUL_END):]
    # Exact legacy generated content needs no duplication. Preserve any other
    # content (including previously edited legacy roles) outside managed policy.
    personal = '' if previous == instructions else previous
    return block + '\n\n## Personal additions (preserved on deploy)\n' + personal


def provision(home, user_home, repo, hermes_bin, policy, run=subprocess.run):
    workers = policy['workers']
    if not isinstance(workers, dict) or not workers:
        raise ValueError('Swarm workers must be a non-empty mapping')
    for name in workers:
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,40}', name):
            raise ValueError('Invalid managed Swarm worker id')
    base = load_config(validated_config_path(home / 'config.yaml', home))
    model = base.get('model', {})
    if not all(isinstance(model.get(k), str) and model[k].strip() for k in ('provider', 'default')):
        raise ValueError('Configure the shared Hermes model before Swarm provisioning')
    for parent in (home / 'profiles', home / 'swarm', home / 'swarm/worktrees', user_home / '.local/bin'):
        if parent.is_symlink():
            raise ValueError('Managed Swarm directories must not be symlinks')
    for name in workers:
        profile = home / 'profiles' / name
        wrapper = user_home / '.local/bin' / name
        if profile.is_symlink() or (profile.exists() and not (profile / '.managed-swarm').is_file()):
            raise ValueError('Refusing to overwrite an unmanaged Swarm profile')
        if wrapper.is_symlink() or (wrapper.exists() and MARKER not in wrapper.read_text()):
            raise ValueError('Refusing to overwrite an unmanaged Swarm wrapper')

    def git(directory, *args):
        return run(['git', '-C', str(directory), *args], check=True,
                   capture_output=True, text=True, timeout=60).stdout.strip()

    common = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    changes = 0
    roster = []
    for name, info in workers.items():
        profile = home / 'profiles' / name
        worktree = home / 'swarm/worktrees' / name
        if worktree.is_symlink():
            raise ValueError('Swarm worktree must not be a symlink')
        if worktree.exists():
            if Path(git(worktree, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() != common:
                raise ValueError('Swarm worktree belongs to a different repository')
        else:
            worktree.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            git(repo, 'worktree', 'add', '--detach', str(worktree), 'HEAD')
            changes += 1
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        changes += save_text(profile / '.managed-swarm', MARKER + '\n')
        path = validated_config_path(profile / 'config.yaml', profile)
        config = load_config(path) if path.exists() else deepcopy(base)
        previous = deepcopy(config)
        if not path.exists():
            config.pop('description', None)
            config.pop('system_prompt', None)
        if not config.get('description'):
            config['description'] = info.get('description') or f'{info["role"]}: {info["instructions"]}'
        config.setdefault('model', {}).update(provider=model['provider'], default=model['default'])
        config.setdefault('delegation', {}).update(provider=model['provider'], model=model['default'])
        config.setdefault('terminal', {})['cwd'] = str(worktree)
        config.setdefault('approvals', {})['mode'] = 'manual'
        if config != previous or not path.exists():
            write_config(path, config)
            changes += 1
        for shared in ('.env', 'auth.json'):
            source, target = home / shared, profile / shared
            if source.exists() and not target.exists() and not target.is_symlink():
                target.symlink_to(source)
                changes += 1
        instructions = f'{MARKER}\n# {info["role"]}\n\n{info["instructions"]}\n\n{RULES}'
        soul_path = profile / 'SOUL.md'
        if soul_path.is_symlink():
            raise ValueError('Managed Swarm SOUL must not be a symlink')
        existing_soul = soul_path.read_text() if soul_path.exists() else ''
        changes += save_text(soul_path, role_soul(existing_soul, instructions))
        escaped_cwd = str(worktree).replace("'", "'\\''")
        wrapper = f'''#!/bin/sh
{MARKER}
export HERMES_HOME={shlex.quote(str(profile))}
cd '{escaped_cwd}' || exit 1
if [ "$#" -eq 0 ]; then set -- chat --tui; fi
exec {shlex.quote(str(hermes_bin))} "$@"
'''
        changes += save_text(user_home / '.local/bin' / name, wrapper, 0o700)
        roster.append({'id': name, 'name': info['role'], 'role': info['role'],
                       'profile': name, 'wrapper': name, 'defaultCwd': str(worktree),
                       'model': model['provider'] + '/' + model['default'],
                       'mission': info['instructions'], 'maxConcurrentTasks': 1,
                       'reviewRequired': name == 'builder', 'acceptsBroadcast': False,
                       'greenlightRequiredFor': ['commit', 'push', 'merge', 'deploy', 'destructive', 'external-send']})
    changes += save_text(home / 'swarm/swarm.yaml', yaml.safe_dump({'version': 1, 'workers': roster}, sort_keys=False))
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('home', 'user-home', 'repo', 'hermes-bin', 'settings'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        settings = yaml.safe_load(sys.stdin.read() if args.settings == Path('-') else args.settings.read_text())
        count = provision(args.home, args.user_home, args.repo, args.hermes_bin, settings['vps_workspace_ui']['swarm'])
        print(f'Swarm provisioned: {count} change(s)')
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # Child stderr/config values can contain credentials; never echo them.
        parser.exit(1, f'Swarm provisioning failed ({type(error).__name__}); check paths, ownership and repository access.\n')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Reconcile the existing private SSH policy without rewriting a correct UFW ruleset."""
import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess


PRIVATE = 'Hermes private SSH'
PUBLIC = 'Hermes deny public SSH'


def run_ufw(args):
    return subprocess.run(['ufw', *args], check=True, capture_output=True, text=True,
                          timeout=30, env={**os.environ, 'LC_ALL': 'C'}).stdout


def private_rules_current(status, interface, port, ipv6):
    families = {False: [], True: []}
    for line in status.splitlines():
        match = re.match(r'^\[\s*\d+\]\s*(.*)$', line)
        if match:
            row = ' '.join(match[1].split())
            families['(v6)' in row].append(row)
    for version in (False, True) if ipv6 else (False,):
        suffix = ' (v6)' if version else ''
        expected = [f'{port}/tcp{suffix} on {interface} ALLOW IN Anywhere{suffix} # {PRIVATE}',
                    f'{port}/tcp{suffix} DENY IN Anywhere{suffix} # {PUBLIC}']
        rows = families[version]
        if rows[:2] != expected or any(row.endswith(('# ' + PRIVATE, '# ' + PUBLIC)) for row in rows[2:]):
            return False
    return True


def replace_private_rules(interface, port):
    # show added also works while UFW is inactive. Delete by complete rule,
    # which removes both address families; never delete unrelated rules.
    for line in run_ufw(['show', 'added']).splitlines():
        if not line.startswith('ufw '):
            continue
        args = shlex.split(line)
        if len(args) >= 4 and args[-2] == 'comment' and args[-1] in (PRIVATE, PUBLIC):
            run_ufw(['--force', 'delete', *args[1:]])
    # Prepend is defined per address family, unlike global numbered insert.
    # Add the deny first so the final allow is first for BOTH IPv4 and IPv6.
    run_ufw(['prepend', 'deny', 'in', 'to', 'any', 'port', str(port), 'proto', 'tcp', 'comment', PUBLIC])
    run_ufw(['prepend', 'allow', 'in', 'on', interface, 'to', 'any', 'port', str(port),
             'proto', 'tcp', 'comment', PRIVATE])


def current_policies(status):
    if 'Status: inactive' in status:
        return None
    match = re.search(r'^Default: (allow|deny|reject) \(incoming\), (allow|deny|reject) \(outgoing\)',
                      status, re.MULTILINE)
    if 'Status: active' not in status or not match:
        raise RuntimeError('Cannot determine the current UFW default policies')
    return match[1], match[2]


def reconcile(interface, port, incoming, outgoing, ipv6):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', interface) or not 1 <= port <= 65535:
        raise ValueError('Invalid private SSH interface or port')
    if incoming not in ('deny', 'reject') or outgoing not in ('allow', 'deny', 'reject'):
        raise ValueError('Unsupported private SSH firewall policies')
    policies = current_policies(run_ufw(['status', 'verbose']))
    status = run_ufw(['status', 'numbered'])
    changed = not private_rules_current(status, interface, port, ipv6)
    if changed:
        replace_private_rules(interface, port)
    for index, (policy, direction) in enumerate(((incoming, 'incoming'), (outgoing, 'outgoing'))):
        if policies is None or policies[index] != policy:
            run_ufw(['default', policy, direction])
            changed = True
    if policies is None:
        run_ufw(['--force', 'enable'])
        changed = True
    if changed and (not private_rules_current(run_ufw(['status', 'numbered']), interface, port, ipv6)
                    or current_policies(run_ufw(['status', 'verbose'])) != (incoming, outgoing)):
        raise RuntimeError('UFW verification failed after applying private SSH policy')
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', required=True)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--incoming', required=True)
    parser.add_argument('--outgoing', required=True)
    args = parser.parse_args()
    try:
        defaults = Path('/etc/default/ufw').read_text()
        match = re.search(r'^IPV6=(yes|no)\s*$', defaults, re.MULTILINE)
        if not match:
            raise ValueError('Cannot determine UFW IPv6 policy from /etc/default/ufw')
        changed = reconcile(args.interface, args.port, args.incoming, args.outgoing, match[1] == 'yes')
        print(f'firewall-changed={str(changed).lower()}')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f'Private SSH firewall reconciliation failed: {error}\n')


if __name__ == '__main__':
    main()

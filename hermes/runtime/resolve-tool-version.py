#!/usr/bin/env python3
"""Resolve a deploy-time latest CLI version, preserving explicit compatible pins."""
import argparse
import json
import re
import sys
import time
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

PACKAGES = ('agent-reach', 'agent-browser', '@googleworkspace/cli')


def fetch_json(url):
    for attempt in range(3):
        try:
            request = Request(url, headers={'User-Agent': 'hermes-deploy', 'Accept': 'application/json'})
            with urlopen(request, timeout=15) as response:
                return json.loads(response.read(5 * 1024 * 1024))
        except (URLError, OSError, ValueError) as exc:
            if attempt == 2:
                raise RuntimeError(f'latest version lookup failed for {url}: {type(exc).__name__}') from exc
            time.sleep(attempt + 1)


def resolve(package, requested):
    if package not in PACKAGES:
        raise ValueError('unsupported managed CLI')
    version = requested
    if requested == 'latest':
        if package == 'agent-reach':
            data = fetch_json('https://api.github.com/repos/Panniantong/Agent-Reach/commits/main')
            # Validate response shape - must be a dict with a valid SHA
            if not isinstance(data, dict):
                raise ValueError('unexpected response structure for agent-reach latest')
            # Validate repository and ref
            repo_full_name = data.get('repository', {}).get('full_name', '')
            if repo_full_name != 'Panniantong/Agent-Reach':
                raise ValueError('agent-reach response repository mismatch')
            ref = data.get('ref', '')
            if ref != 'refs/heads/main':
                raise ValueError('agent-reach response ref mismatch')
            version = data.get('sha', '')
        else:
            version = fetch_json('https://registry.npmjs.org/' + quote(package, safe='@')).get('dist-tags', {}).get('latest', '')
    pattern = r'[0-9a-f]{40}' if package == 'agent-reach' else r'[0-9]+\.[0-9]+\.[0-9]+'
    if not isinstance(version, str) or not re.fullmatch(pattern, version):
        raise ValueError(f'invalid resolved version for {package}')
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, choices=PACKAGES)
    parser.add_argument('--requested', required=True)
    args = parser.parse_args()
    try:
        print(resolve(args.package, args.requested))
    except (ValueError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

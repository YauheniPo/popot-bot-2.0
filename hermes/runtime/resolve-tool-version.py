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
GITHUB_API_URL = 'https://api.github.com/repos/Panniantong/Agent-Reach/commits/main'
NPM_REGISTRY_URL = 'https://registry.npmjs.org/'
AGENT_REACH_SHA_PATTERN = r'[0-9a-f]{40}'
NPM_VERSION_PATTERN = r'[0-9]+\.[0-9]+\.[0-9]+'


def fetch_json(url):
    for attempt in range(3):
        try:
            request = Request(url, headers={'User-Agent': 'hermes-deploy', 'Accept': 'application/json'})
            with urlopen(request, timeout=15) as response:
                data = response.read(5 * 1024 * 1024)
                # Cap response size at read level (5 MiB)
                return json.loads(data)
        except (URLError, OSError, ValueError) as exc:
            if attempt == 2:
                raise RuntimeError(f'latest version lookup failed for {url}: {type(exc).__name__}') from exc
            time.sleep(attempt + 1)


def _resolve_agent_reach():
    data = fetch_json(GITHUB_API_URL)
    if not isinstance(data, dict):
        raise ValueError('unexpected response structure for agent-reach latest')
    repo_full_name = data.get('repository', {}).get('full_name', '')
    if repo_full_name != 'Panniantong/Agent-Reach':
        raise ValueError('agent-reach response repository mismatch')
    ref = data.get('ref', '')
    if ref != 'refs/heads/main':
        raise ValueError('agent-reach response ref mismatch')
    return data.get('sha', '')


def _resolve_npm_package(package):
    url = NPM_REGISTRY_URL + quote(package, safe='@')
    data = fetch_json(url)
    if not isinstance(data, dict):
        raise ValueError('unexpected npm response structure')
    return data.get('dist-tags', {}).get('latest', '')


def resolve(package, requested):
    if package not in PACKAGES:
        raise ValueError('unsupported managed CLI')
    version = requested
    if requested == 'latest':
        if package == 'agent-reach':
            version = _resolve_agent_reach()
        else:
            version = _resolve_npm_package(package)
    pattern = AGENT_REACH_SHA_PATTERN if package == 'agent-reach' else NPM_VERSION_PATTERN
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
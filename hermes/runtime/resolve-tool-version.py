#!/usr/bin/env python3
"""Resolve a deploy-time latest CLI version, preserving explicit compatible pins."""
import argparse
import json
import re
import sys
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

PACKAGES = ('agent-reach', 'agent-browser', '@googleworkspace/cli')
AGENT_REACH_COMMITS_URL = 'https://api.github.com/repos/Panniantong/Agent-Reach/commits/'
GITHUB_API_URL = AGENT_REACH_COMMITS_URL + 'main'
NPM_REGISTRY_URL = 'https://registry.npmjs.org/'
AGENT_REACH_SHA_PATTERN = r'[0-9a-f]{40}'
NPM_VERSION_PATTERN = r'[0-9]+\.[0-9]+\.[0-9]+'


def fetch_json(url):
    for attempt in range(3):
        try:
            request = Request(url, headers={'User-Agent': 'hermes-deploy', 'Accept': 'application/json'})
            with urlopen(request, timeout=15) as response:
                # Read one extra byte to reject oversized responses before JSON parsing.
                data = response.read(5 * 1024 * 1024 + 1)
                if len(data) > 5 * 1024 * 1024:
                    raise ValueError('latest version response exceeds 5 MiB')
                return json.loads(data)
        except (OSError, ValueError) as exc:
            if attempt == 2:
                raise RuntimeError(f'latest version lookup failed for {url}: {type(exc).__name__}') from exc
            time.sleep(attempt + 1)


def _resolve_agent_reach():
    data = fetch_json(GITHUB_API_URL)
    if not isinstance(data, dict):
        raise ValueError('unexpected response structure for agent-reach latest')
    # Get a commit returns sha/url, not repository/ref. The request fixes the
    # branch; bind its result to this repository and SHA before constructing a URL.
    version = data.get('sha', '')
    if not isinstance(version, str) or not re.fullmatch(AGENT_REACH_SHA_PATTERN, version):
        raise ValueError('invalid resolved version for agent-reach')
    if data.get('url') != AGENT_REACH_COMMITS_URL + version:
        raise ValueError('agent-reach response commit URL mismatch')
    return version


def _resolve_npm_package(package):
    data = fetch_json(NPM_REGISTRY_URL + quote(package, safe='@'))
    if not isinstance(data, dict):
        raise ValueError('unexpected npm response structure')
    tags = data.get('dist-tags', {})
    if not isinstance(tags, dict):
        raise ValueError('unexpected npm dist-tags structure')
    return tags.get('latest', '')


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

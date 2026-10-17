"""Pure state comparisons for idempotent deployment; no remote mutations."""
import re


def serve_matches(current, dns_name, services):
    if not isinstance(dns_name, str) or not re.fullmatch(r'[A-Za-z0-9.-]+', dns_name):
        raise ValueError('Tailscale DNS name is required before comparing Serve configuration')
    if current is None:
        current = {}
    if not isinstance(current, dict):
        raise ValueError('Tailscale Serve status must be a mapping')
    tcp, web = {}, {}
    for service in services:
        port = str(service['port'])
        if port in tcp:
            raise ValueError('Duplicate Tailscale Serve port')
        tcp[port] = {service['protocol'].upper(): True}
        web[f'{dns_name.rstrip(".")}:{port}'] = {'Handlers': {'/': {'Proxy': service['target']}}}
    # Compare the whole managed set, including stale routes and public Funnel.
    # Empty optional maps and explicit false Funnel flags do not change behavior.
    other = {key: value for key, value in current.items()
             if key not in ('TCP', 'Web', 'AllowFunnel') and value}
    funnel = current.get('AllowFunnel') or {}
    if not isinstance(funnel, dict):
        raise ValueError('Tailscale AllowFunnel must be a mapping')
    return (not other and not any(funnel.values()) and
            (current.get('TCP') or {}) == tcp and (current.get('Web') or {}) == web)


def _preserve_key(candidate, existing, key):
    parts = key.split('.')
    for part in parts[:-1]:
        if not isinstance(candidate.get(part), dict):
            return
        candidate[part] = dict(candidate[part])
        candidate = candidate[part]
        existing = existing.get(part, {})
        if not isinstance(existing, dict):
            existing = {}
    if parts[-1] in existing:
        candidate[parts[-1]] = existing[parts[-1]]
    else:
        candidate.pop(parts[-1], None)


def preserve_runtime_capabilities(candidate, existing, runtime, capabilities):
    """Let the native runtime step own capability settings, without resetting them first."""
    result = dict(candidate)
    for name, rules in runtime.get('capabilities', {}).items():
        if name in capabilities:
            for key in rules.get('set', {}):
                _preserve_key(result, existing, key)
    return result


class FilterModule:
    def filters(self):
        return {'hermes_serve_matches': serve_matches,
                'hermes_preserve_runtime_capabilities': preserve_runtime_capabilities}

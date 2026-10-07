"""Validate provider/model exclusions shared by deployment and native fallback."""

import re


def fallback_excluded_pairs(policy):
    if not isinstance(policy, dict):
        raise ValueError('fallback_policy must be a mapping')
    routes = policy.get('excluded_routes', [])
    if not isinstance(routes, list):
        raise ValueError('fallback_policy.excluded_routes must be a list')
    pairs = set()
    for route in routes:
        if not isinstance(route, dict) or set(route) != {'provider', 'model'}:
            raise ValueError('fallback exclusions require only provider and model')
        provider, model = route['provider'], route['model']
        if (not isinstance(provider, str) or not re.fullmatch(r'[a-z][a-z0-9-]*', provider)
                or not isinstance(model, str) or len(model) > 200
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:+-]*', model)
                or '://' in model):
            raise ValueError('fallback exclusions require valid provider and model')
        pair = (provider, model)
        if pair in pairs:
            raise ValueError('fallback exclusions contain a duplicate provider/model pair')
        pairs.add(pair)
    return pairs

"""Managed additions embedded in pinned hermes_cli.fallback_config on deploy.

No provider calls or tool replay: the native agent owns credentials, clients,
context adaptation, cooldown and the continuation of the current API request.
"""

import copy
import re

import yaml


class FallbackCommandError(ValueError):
    """Safe, authored command feedback (never raw config or provider errors)."""


def _managed_provider(value):
    from hermes_cli.providers import ALIASES
    provider = str(value or '').strip().lower()
    return ALIASES.get(provider, provider)


def validate_fallback_routes(routes, allowed):
    """Chat routes never accept inline keys, arbitrary endpoints or paid OpenRouter."""
    if not isinstance(routes, list) or len(routes) > 8:
        raise FallbackCommandError('Нужно от 0 до 8 резервных маршрутов.')
    result, seen = [], set()
    for route in routes:
        if not isinstance(route, dict) or set(route) != {'provider', 'model'}:
            raise FallbackCommandError('Маршрут: provider model; ключи и URL через чат не принимаются.')
        provider, model = route['provider'], route['model']
        if (not isinstance(provider, str) or provider not in allowed
                or not isinstance(model, str) or len(model) > 200
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:+-]*', model)
                or '://' in model):
            raise FallbackCommandError('Недопустимый provider/model. Провайдер должен быть разрешён в deploy-конфиге.')
        if provider == 'openrouter' and not model.endswith(':free'):
            raise FallbackCommandError('Для OpenRouter разрешены только модели с суффиксом :free.')
        identity = (provider, model)
        if identity in seen:
            raise FallbackCommandError('Список содержит повторный маршрут.')
        seen.add(identity)
        result.append(dict(route))
    return result


def _fallback_route_text(routes):
    # Never display arbitrary extra fields such as api_key/base_url.
    return '\n'.join(f'{i}. {r.get("provider", "?")} {r.get("model", "?")}'
                     for i, r in enumerate(routes, 1)) or '(отключён)'


def edit_fallback_config(config, arguments):
    """Pure command parser; None means read-only, otherwise a fresh raw config."""
    policy = config.get('fallback_policy', {})
    allowed = policy.get('allowed_providers', [])
    if not isinstance(allowed, list) or any(not isinstance(p, str) for p in allowed):
        raise ValueError('Invalid managed provider policy')
    chain = config.get('fallback_providers', [])
    command, _, value = arguments.strip().partition(' ')
    if command in ('', 'list') and not value:
        return None, ('Резервные маршруты:\n' + _fallback_route_text(chain)
                      + ('\nТакже задан legacy fallback_model; set/off/reset заменят его.'
                         if config.get('fallback_model') else '')
                      + '\nРазрешённые providers: ' + ', '.join(allowed)
                      + '\n/fallback set provider model; provider model'
                      + '\n/fallback add provider model | remove N | off | reset'
                      + '\nПри quota/429 выбирается другой provider. Проверка API — при использовании.')
    if '\n' in arguments or '\r' in arguments:
        raise FallbackCommandError('Команда должна занимать одну строку.')
    if command in ('set', 'add') and value:
        entries = [part.split() for part in value.split(';')]
        if any(len(entry) != 2 for entry in entries):
            raise FallbackCommandError('Используйте: /fallback set provider model; provider model')
        new = [{'provider': p, 'model': m} for p, m in entries]
        routes = (chain if command == 'add' else []) + new
    elif command == 'remove' and value.isdecimal() and 1 <= int(value) <= len(chain):
        routes = [entry for i, entry in enumerate(chain, 1) if i != int(value)]
    elif command == 'off' and not value:
        routes = []
    elif command == 'reset' and not value and 'default_routes' in policy:
        routes = policy['default_routes']
    else:
        raise FallbackCommandError('Используйте /fallback: list, set, add, remove N, off или reset.')
    routes = validate_fallback_routes(routes, allowed)
    updated = copy.deepcopy(config)
    updated['fallback_providers'] = routes
    updated.pop('fallback_model', None)  # native legacy chain must not re-enable off
    return updated, ('Список сохранён для этого Hermes profile/home со следующего сообщения.'
                     '\nУже выполняющиеся задачи не переключаются.'
                     '\n' + _fallback_route_text(routes))


def run_fallback_command(config_path, arguments):
    """Use the native fail-closed, atomic config writer in the routed home."""
    from hermes_cli.config import _CONFIG_LOCK, atomic_config_write, read_user_config_raw
    try:
        with _CONFIG_LOCK:
            config = read_user_config_raw(config_path)
            updated, reply = edit_fallback_config(config, arguments)
            if updated is not None:
                atomic_config_write(config_path, updated)
            return reply
    except FallbackCommandError as error:
        return str(error)
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, yaml.YAMLError):
        # Config/credential values must never leak through an exception message.
        return 'Не удалось прочитать/сохранить fallback. Проверьте config.yaml и права доступа.'


def begin_fallback_walk(agent, reason):
    """Keep quota failures for this native fallback walk, not a permanent blacklist."""
    if getattr(agent, '_fallback_index', 0) == 0 and not getattr(agent, '_fallback_activated', False):
        agent._managed_quota_providers = set()
    if getattr(reason, 'value', reason) in {'rate_limit', 'upstream_rate_limit', 'billing'}:
        blocked = set(getattr(agent, '_managed_quota_providers', ()))
        blocked.add(_managed_provider(getattr(agent, 'provider', '')))
        agent._managed_quota_providers = blocked


def allow_fallback_candidate(agent, candidate):
    provider = _managed_provider(candidate.get('provider'))
    if provider == 'openrouter' and not str(candidate.get('model', '')).endswith(':free'):
        return False
    return provider not in getattr(agent, '_managed_quota_providers', ())

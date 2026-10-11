"""One durable chat-selected model route for agent cron jobs in every profile."""

from urllib.parse import urlsplit


def _shared_config():
    from hermes_constants import get_default_hermes_root
    from hermes_cli.config import require_readable_config_before_write

    path = get_default_hermes_root() / 'config.yaml'
    if path.is_symlink():
        raise ValueError('shared cron config must not be a symlink')
    config = require_readable_config_before_write(path)
    cron = config.get('cron') or {}
    if not isinstance(cron, dict):
        raise ValueError('shared cron config must be a mapping')
    return path, cron


def _route(value):
    if not isinstance(value, dict) or any(
        not isinstance(value.get(key), str) or not value[key].strip()
        for key in ('model', 'provider')
    ):
        raise ValueError('shared cron model requires provider and model')
    base_url = value.get('base_url') or None
    if base_url is not None:
        if not isinstance(base_url, str):
            raise ValueError('shared cron model base_url must be a URL')
        parsed = urlsplit(base_url)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('shared cron model requires a credential-free HTTP(S) URL')
    # Never copy API keys or arbitrary model-switch metadata into shared config.
    return {'model': value['model'], 'provider': value['provider'], 'base_url': base_url}


def persist_chat_model(selection):
    """Persist an explicit permanent choice after the chat runtime switched successfully."""
    path, cron = _shared_config()
    if cron.get('follow_chat_model') is not True:
        return False
    route = _route({'model': selection.new_model, 'provider': selection.target_provider,
                    'base_url': selection.base_url})
    if cron.get('shared_model') != route:
        from utils import atomic_roundtrip_yaml_update
        # One atomic object update: a cron tick sees either complete route, never
        # the new model paired with an old provider. Other cron settings survive.
        atomic_roundtrip_yaml_update(path, 'cron.shared_model', route)
    return True


def routed_job(job):
    """Snapshot the shared route for this run; leave stored jobs and running work alone."""
    if job.get('no_agent'):
        return job
    _path, cron = _shared_config()
    if cron.get('follow_chat_model') is not True:
        return job
    return {**job, **_route(cron.get('shared_model'))}

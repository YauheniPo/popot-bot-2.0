#!/usr/bin/env python3
"""Use pinned Hermes' native unit generator and reinstall only on actual drift."""
import argparse
from pathlib import Path


def ensure_unit(gateway, user):
    path = gateway.get_systemd_unit_path(system=True)
    if path.is_symlink():
        raise RuntimeError('Managed gateway unit must not be a symlink')
    existing_home = gateway._hermes_home_pinned_by_unit(path) if path.exists() else None
    if existing_home and Path(existing_home).resolve() != gateway.get_hermes_home().resolve():
        raise RuntimeError('Existing gateway unit belongs to another Hermes home; refusing to rewrite it')
    expected = gateway.generate_systemd_unit(system=True, run_as_user=user)

    def normalize(text):
        return gateway._normalize_service_definition(gateway._strip_optional_systemd_directives(text))

    changed = not path.exists() or normalize(path.read_text()) != normalize(expected)
    if changed:
        gateway.systemd_install(force=True, system=True, run_as_user=user,
                                enable_on_startup=True, non_interactive=True)
        if not path.exists() or normalize(path.read_text()) != normalize(expected):
            raise RuntimeError('Gateway unit verification failed after native installation')
    else:
        gateway._ensure_system_service_linger(user)
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True)
    args = parser.parse_args()
    from hermes_cli import gateway
    changed = ensure_unit(gateway, args.user)
    print(f'gateway-unit-changed={str(changed).lower()}')


if __name__ == '__main__':
    main()

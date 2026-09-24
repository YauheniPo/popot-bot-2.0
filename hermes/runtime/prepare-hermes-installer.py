#!/usr/bin/env python3
"""Adapt the checksum-verified upstream installer for a managed pinned update."""

import sys


PATCHES = (
    (
        '            local autostash_ref=""\n',
        '''            local autostash_ref=""
            # HERMES MANAGED: only our own interrupted runs block a retry.
            # Historical upstream stashes remain untouched, not auto-restored.
            if [ -n "$(git stash list --format=%gs --grep=hermes-managed-install-autostash-)" ]; then
                log_error "Pending managed installer stash: restore/reconcile it before retrying the managed update."
                return 1
            fi
''',
    ),
    (
        '                stash_name="hermes-install-autostash-$(date -u +%Y%m%d-%H%M%S)"\n',
        '                stash_name="hermes-managed-install-autostash-$(date -u +%Y%m%d-%H%M%S)"\n',
    ),
    (
        '                    git reset -q\n',
        '''                    log_error "Unresolved Git conflicts: reconcile them before retrying the managed update."
                    return 1
''',
    ),
    (
        '''            git remote set-branches origin "$BRANCH" 2>/dev/null || true
            git fetch origin "$BRANCH"
            git checkout "$BRANCH"
            # Managed installs should follow origin/$BRANCH exactly. If the
            # checkout has diverged (or has local-only commits), ff-only pull
            # cannot succeed — mirror ``hermes update`` and reset to the
            # fetched remote so bootstrap/install can recover.
            if ! git pull --ff-only origin "$BRANCH"; then
                log_warn "Fast-forward not possible; resetting managed install to origin/$BRANCH..."
                git reset --hard "origin/$BRANCH"
            fi
''',
        '''            # HERMES MANAGED: restore local changes only onto the pinned source.
            if [ -z "$INSTALL_COMMIT" ] || [ "$FORCE_COMMIT" != true ]; then
                log_error "Managed installer requires --commit and --force-commit."
                return 1
            fi
            git fetch origin "$INSTALL_COMMIT"
            git checkout --detach "$INSTALL_COMMIT"
''',
    ),
    (
        '''                        git reset --hard HEAD >/dev/null 2>&1 || true
                        log_info "Working tree reset to clean state."
                        log_info "Restore your changes later with: git stash apply $autostash_ref"
''',
        '''                        log_error "Managed update aborted; stash and conflicted working tree preserved."
                        return 1
''',
    ),
)


def prepare(source: str) -> str:
    # Validate every anchor before writing anything. A changed upstream layout
    # must stop the deployment, not silently retain unsafe update behavior.
    for old, new in PATCHES:
        if source.count(new) == 1:
            continue
        if source.count(old) != 1:
            raise ValueError("unsupported upstream installer layout")
        source = source.replace(old, new, 1)
    return source


def main() -> int:
    if len(sys.argv) != 1:
        print("Usage: prepare-hermes-installer.py < INSTALLER > PREPARED", file=sys.stderr)
        return 2
    try:
        prepared = prepare(sys.stdin.read())
        sys.stdout.write(prepared)
    except (OSError, ValueError) as error:
        print(f"ERROR: cannot prepare pinned installer: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

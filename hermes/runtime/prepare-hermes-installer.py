#!/usr/bin/env python3
"""Adapt the checksum-verified upstream installer for a managed pinned update."""

import sys


PATCHES = (
    (
        'stage_repository() {\n    local pinned=false channel="" release="" origin\n',
        '''stage_repository() {
    local pinned=false channel="" release="" origin
    # HERMES MANAGED: update directly to the pin and restore only this run's stash.
    [ -n "$INSTALL_COMMIT" ] || { log_error "Managed installer requires --commit."; return 1; }
    if [ -e "$INSTALL_DIR/.git" ]; then
        git -C "$INSTALL_DIR" rev-parse --verify HEAD >/dev/null 2>&1 || {
            log_error "Unusable Git checkout: reconcile it before retrying the managed update."
            return 1
        }
        if [ -n "$(git -C "$INSTALL_DIR" stash list --format=%gs --grep=hermes-managed-install-autostash-)" ]; then
            log_error "Pending managed installer stash: restore/reconcile it before retrying the managed update."
            return 1
        fi
        if [ -n "$(git -C "$INSTALL_DIR" ls-files --unmerged)" ]; then
            log_error "Unresolved Git conflicts: reconcile them before retrying the managed update."
            return 1
        fi
        if [ "$(git -C "$INSTALL_DIR" rev-parse HEAD)" != "$INSTALL_COMMIT" ] \
                && [ -n "${HERMES_MANAGED_PATCH_PREPARER:-}" ]; then
            python3 "$HERMES_MANAGED_PATCH_PREPARER" "$INSTALL_DIR" || return 1
        fi
        local autostash_ref=""
        if [ -n "$(git -C "$INSTALL_DIR" status --porcelain)" ]; then
            git -C "$INSTALL_DIR" stash push --include-untracked \
                -m "hermes-managed-install-autostash-$(date -u +%Y%m%d-%H%M%S)" || return 1
            autostash_ref="$(git -C "$INSTALL_DIR" rev-parse refs/stash)" || return 1
        fi
        git -C "$INSTALL_DIR" fetch origin "$INSTALL_COMMIT" || return 1
        git -C "$INSTALL_DIR" checkout --detach "$INSTALL_COMMIT" || return 1
        if [ -n "$autostash_ref" ]; then
            if ! git -C "$INSTALL_DIR" stash apply "$autostash_ref"; then
                log_error "Managed update aborted; stash and conflicted working tree preserved."
                return 1
            fi
            # A concurrent user stash must never be dropped in place of ours.
            if [ "$(git -C "$INSTALL_DIR" rev-parse refs/stash)" != "$autostash_ref" ]; then
                log_error "Stash changed during update; reconcile the managed stash before retrying."
                return 1
            fi
            git -C "$INSTALL_DIR" stash drop 'stash@{0}' || return 1
        fi
        return 0
    fi
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

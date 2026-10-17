# shellcheck shell=bash

mark_ops_change() {
    OPS_CHANGED=true
    local unit="${1:-}"
    [[ -n "$unit" ]] || return 0
    [[ "$unit" =~ ^[a-zA-Z0-9_.@:-]+\.(service|timer)$ ]] || die "invalid managed restart unit"
    [[ ! -L "$OPS_RESTART_DIR" ]] || die "restart state directory must not be a symlink"
    install -d -o root -g root -m 0700 "$OPS_RESTART_DIR"
    # Record before writing: a failed copy/deploy/restart must be retried on the
    # next deployment even if the destination already has the new contents.
    touch "$OPS_RESTART_DIR/$unit"
}

install_managed_file() {
    local source="$1" target="$2" mode="$3" owner="$4" group="$5" unit="${6:-}"
    if [[ -L "$target" || ! -f "$target" ]] || ! cmp -s "$source" "$target"; then
        mark_ops_change "$unit"
        install -o "$owner" -g "$group" -m "$mode" "$source" "$target"
    else
        # Repair access drift without replacing unchanged files/inodes.
        if [[ "$(stat -c '%a:%U:%G' -- "$target")" != "${mode#0}:$owner:$group" ]]; then
            mark_ops_change "$unit"
            chown "$owner:$group" "$target"
            chmod "$mode" "$target"
        fi
    fi
}

report_ops_changes() {
    local unit units
    units="$(managed_services gateway)"
    units+=$'\n'"$(managed_services observability)"
    units+=$'\n'"$(managed_services timers)"
    while IFS= read -r unit; do
        if [[ -f "$OPS_RESTART_DIR/$unit" ]]; then
            printf 'ops-restart=%s\n' "$unit"
            OPS_CHANGED=true
        fi
    done <<< "$units"
    printf 'ops-changed=%s\n' "$OPS_CHANGED"
}

acknowledge_ops_restarts() {
    local unit units
    units="$(managed_services observability)"
    if [[ "$START_TIMERS" == true ]]; then
        units+=$'\n'"$(managed_services gateway)"
        units+=$'\n'"$(managed_services timers)"
    fi
    while IFS= read -r unit; do
        rm -f -- "$OPS_RESTART_DIR/$unit"
    done <<< "$units"
}

render() {
    local source="$1"
    local target="$2"
    local mode="$3"
    local temporary
    local hermes_python="${HERMES_HOME}/hermes-agent/venv/bin/python"
    local settings_file="${SCRIPT_DIR}/../config/vps-defaults.yml"
    local config_applier="${SCRIPT_DIR}/../runtime/apply-config.py"
    temporary="$(mktemp)"
    [[ -f "${settings_file}" ]] || die "VPS settings are missing: ${settings_file}"
    [[ -f "${config_applier}" ]] || die "VPS config renderer is missing: ${config_applier}"
    [[ -x "${hermes_python}" ]] || die "Hermes Python is missing: ${hermes_python}"
    "${hermes_python}" "${config_applier}" render \
        --settings "${settings_file}" \
        --template "${source}" \
        --hermes-user "${HERMES_USER}" \
        --hermes-group "${HERMES_GROUP}" \
        --user-home "${USER_HOME}" \
        --hermes-home "${HERMES_HOME}" \
        --hermes-bin "${HERMES_BIN}" \
        --workspace "${WORKSPACE_DIR}" \
        --backup-dir "${BACKUP_DIR}" >"${temporary}"
    install_managed_file "${temporary}" "${target}" "${mode}" root "${4:-root}" "${5:-}"
    rm -f "${temporary}"
    return
}

managed_services() {
    local group="$1"
    local hermes_python="${HERMES_HOME}/hermes-agent/venv/bin/python"
    local settings_file="${SCRIPT_DIR}/../config/vps-defaults.yml"
    local config_applier="${SCRIPT_DIR}/../runtime/apply-config.py"

    [[ -x "${hermes_python}" ]] || die "Hermes Python is missing: ${hermes_python}"
    [[ -f "${settings_file}" ]] || die "VPS settings are missing: ${settings_file}"
    [[ -f "${config_applier}" ]] || die "VPS config applier is missing: ${config_applier}"
    "${hermes_python}" "${config_applier}" services --settings "${settings_file}" "${group}"
    return
}

managed_value() {
    local key="$1"
    local hermes_python="${HERMES_HOME}/hermes-agent/venv/bin/python"
    local settings_file="${SCRIPT_DIR}/../config/vps-defaults.yml"
    local config_applier="${SCRIPT_DIR}/../runtime/apply-config.py"

    "${hermes_python}" "${config_applier}" value --settings "${settings_file}" "${key}"
    return
}

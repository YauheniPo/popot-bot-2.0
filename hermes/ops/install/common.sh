# shellcheck shell=bash

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
    install -o root -g root -m "${mode}" "${temporary}" "${target}"
    rm -f "${temporary}"
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
}

managed_value() {
    local key="$1"
    local hermes_python="${HERMES_HOME}/hermes-agent/venv/bin/python"
    local settings_file="${SCRIPT_DIR}/../config/vps-defaults.yml"
    local config_applier="${SCRIPT_DIR}/../runtime/apply-config.py"

    "${hermes_python}" "${config_applier}" value --settings "${settings_file}" "${key}"
}

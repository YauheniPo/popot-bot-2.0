# shellcheck shell=bash

render() {
    local source="$1"
    local target="$2"
    local mode="$3"
    local temporary
    temporary="$(mktemp)"
    sed \
        -e "s|@HERMES_USER@|${HERMES_USER}|g" \
        -e "s|@HERMES_GROUP@|${HERMES_GROUP}|g" \
        -e "s|@USER_HOME@|${USER_HOME}|g" \
        -e "s|@HERMES_HOME@|${HERMES_HOME}|g" \
        -e "s|@HERMES_BIN@|${HERMES_BIN}|g" \
        "${source}" >"${temporary}"
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


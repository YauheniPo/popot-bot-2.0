# shellcheck shell=bash

enable_observability_plugin() {
    local hermes_python="${HERMES_HOME}/hermes-agent/venv/bin/python"
    local result
    local gateway_service
    local -a configure_args=(
        --config "${HERMES_HOME}/config.yaml"
        --hermes-home "${HERMES_HOME}"
    )
    gateway_service="$(managed_services gateway)"
    configure_args+=(--gateway-service "${gateway_service}")
    [[ -x "${hermes_python}" ]] || {
        log "WARNING: Hermes Python environment is unavailable; the observability plugin was not enabled"
        return 0
    }

    # The regular plugin command can block in a non-interactive provisioner.
    # Keep the small deterministic config mutation in a testable helper.
    if [[ "${VSCODE_ENABLED}" == true ]]; then
        configure_args+=(
            --vscode-enabled
            --vscode-compose-file "${VSCODE_COMPOSE_FILE}"
            --vscode-env-file "${VSCODE_ENV_FILE}"
            --vscode-project-name "${VSCODE_PROJECT_NAME}"
        )
    fi

    result="$(runuser -u "${HERMES_USER}" -- env -i \
        HOME="${USER_HOME}" \
        HERMES_HOME="${HERMES_HOME}" \
        PATH="${USER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin" \
        "${hermes_python}" "${SCRIPT_DIR}/configure-plugin.py" \
        "${configure_args[@]}")"
    if [[ "${result}" == "changed" ]]; then
        PLUGIN_CONFIGURATION_CHANGED=true
    fi
}

install_observability_plugin() {
    log "installing the privacy-aware observability plugin"
    install -d -o "${HERMES_USER}" -g "${HERMES_GROUP}" -m 0700 \
        "${HERMES_HOME}/plugins/ops-observability" \
        "${HERMES_HOME}/plugins/ops-observability/dashboard/dist" \
        "${HERMES_HOME}/ops/metrics" \
        "${HERMES_HOME}/logs" \
        "${HERMES_HOME}/state-snapshots" \
        "${BACKUP_DIR}"
    # `install -d` does not reliably correct ownership of an already-existing
    # parent directory.  These paths must remain private to the Hermes service
    # account, including after a previous root-run maintenance command.
    chown "${HERMES_USER}:${HERMES_GROUP}" \
        "${HERMES_HOME}/ops" \
        "${HERMES_HOME}/ops/metrics" \
        "${HERMES_HOME}/logs" \
        "${HERMES_HOME}/state-snapshots" \
        "${BACKUP_DIR}"
    chmod 0700 \
        "${HERMES_HOME}/ops" \
        "${HERMES_HOME}/ops/metrics" \
        "${HERMES_HOME}/logs" \
        "${HERMES_HOME}/state-snapshots" \
        "${BACKUP_DIR}"
    # Hermes itself, the dashboard, and the exporters all run as this unprivileged
    # account.  A previous root-run command can leave the SQLite database behind
    # as root-owned, which prevents the dashboard's read-only API from opening it.
    # Repair only the database and its SQLite sidecar files on every idempotent run.
    find "${HERMES_HOME}/ops" -maxdepth 1 -type f \
        \( -name 'metrics.db' -o -name 'metrics.db-shm' -o -name 'metrics.db-wal' \) \
        -exec chown "${HERMES_USER}:${HERMES_GROUP}" {} + \
        -exec chmod 0600 {} +
    install -o "${HERMES_USER}" -g "${HERMES_GROUP}" -m 0600 /dev/null \
        "${HERMES_HOME}/.backup.lock"

    for plugin_file in \
        plugin.yaml \
        __init__.py \
        billing.py \
        commands.py \
        hooks.py \
        metrics.py \
        privacy.py \
        storage.py \
        dashboard/manifest.json \
        dashboard/plugin_api.py \
        dashboard/dist/index.js \
        dashboard/dist/style.css; do
        if [[ ! -f "${HERMES_HOME}/plugins/ops-observability/${plugin_file}" ]] || \
            ! cmp -s "${SCRIPT_DIR}/plugin/ops-observability/${plugin_file}" "${HERMES_HOME}/plugins/ops-observability/${plugin_file}"; then
            PLUGIN_CONTENT_CHANGED=true
            break
        fi
    done
    install -o "${HERMES_USER}" -g "${HERMES_GROUP}" -m 0600 \
        "${SCRIPT_DIR}/plugin/ops-observability/plugin.yaml" \
        "${SCRIPT_DIR}/plugin/ops-observability/__init__.py" \
        "${SCRIPT_DIR}/plugin/ops-observability/billing.py" \
        "${SCRIPT_DIR}/plugin/ops-observability/commands.py" \
        "${SCRIPT_DIR}/plugin/ops-observability/hooks.py" \
        "${SCRIPT_DIR}/plugin/ops-observability/metrics.py" \
        "${SCRIPT_DIR}/plugin/ops-observability/privacy.py" \
        "${SCRIPT_DIR}/plugin/ops-observability/storage.py" \
        "${HERMES_HOME}/plugins/ops-observability/"
    install -o "${HERMES_USER}" -g "${HERMES_GROUP}" -m 0600 \
        "${SCRIPT_DIR}/plugin/ops-observability/dashboard/manifest.json" \
        "${SCRIPT_DIR}/plugin/ops-observability/dashboard/plugin_api.py" \
        "${HERMES_HOME}/plugins/ops-observability/dashboard/"
    install -o "${HERMES_USER}" -g "${HERMES_GROUP}" -m 0600 \
        "${SCRIPT_DIR}/plugin/ops-observability/dashboard/dist/index.js" \
        "${SCRIPT_DIR}/plugin/ops-observability/dashboard/dist/style.css" \
        "${HERMES_HOME}/plugins/ops-observability/dashboard/dist/"

    if [[ ! -e "${HERMES_HOME}/ops/model-prices.json" ]]; then
        install -o "${HERMES_USER}" -g "${HERMES_GROUP}" -m 0600 \
            "${SCRIPT_DIR}/templates/model-prices.json" "${HERMES_HOME}/ops/model-prices.json"
    fi

    if [[ -x "${HERMES_BIN}" ]]; then
        log "validating and enabling the Hermes plugin"
        if ! timeout --foreground 60s runuser -u "${HERMES_USER}" -- env -i \
            HOME="${USER_HOME}" \
            HERMES_HOME="${HERMES_HOME}" \
            PATH="${USER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin" \
            "${HERMES_BIN}" plugins doctor "${HERMES_HOME}/plugins/ops-observability" --ci </dev/null; then
            log "WARNING: Hermes plugin validation timed out or failed; continuing with the explicit configuration opt-in"
        fi
        enable_observability_plugin
    else
        log "Hermes CLI not found yet; enable later by adding ops-observability to plugins.enabled in config.yaml"
    fi
}

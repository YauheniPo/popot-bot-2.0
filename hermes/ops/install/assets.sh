# shellcheck shell=bash

install_review_metrics_sync() {
    install -d -o root -g root -m 0755 /usr/local/lib/hermes-ops
    [[ ! -L "${HERMES_HOME}/scripts" ]] || die "Hermes scripts directory must not be a symlink"
    install -d -o "${HERMES_USER}" -g "${HERMES_GROUP}" -m 0750 "${HERMES_HOME}/scripts"
    install_managed_file "${SCRIPT_DIR}/sync-ai-review-metrics.py" \
        /usr/local/lib/hermes-ops/sync-ai-review-metrics.py 0755 root root
    install_managed_file "${SCRIPT_DIR}/../runtime/github-cli-wrapper.py" \
        /usr/local/lib/hermes-ops/github-cli-wrapper.py 0755 root root
    install_managed_file "${SCRIPT_DIR}/cron/hermes-ai-review-metrics-sync.py" \
        "${HERMES_HOME}/scripts/hermes-ai-review-metrics-sync.py" 0755 "${HERMES_USER}" "${HERMES_GROUP}"
    local result
    result="$(timeout 60s runuser -u "${HERMES_USER}" -- env HOME="${USER_HOME}" HERMES_HOME="${HERMES_HOME}" \
        "${HERMES_HOME}/hermes-agent/venv/bin/python" "${SCRIPT_DIR}/../runtime/apply-config.py" review-metrics-job \
        --settings "${SCRIPT_DIR}/../config/vps-defaults.yml" --hermes-home "${HERMES_HOME}")"
    printf '%s\n' "$result"
    if [[ "$result" == *review-metrics-job-changed=true* ]]; then
        mark_ops_change
    fi

    return
}

install_operations_assets() {
    local gateway_service
    gateway_service="$(managed_services gateway)"
    log "installing health checks, metrics exporter, and reporting CLI"
    install -d -o root -g root -m 0755 /usr/local/lib/hermes-ops
    local asset
    for asset in \
        "${SCRIPT_DIR}/health-check.sh" \
        "${SCRIPT_DIR}/backup.sh" \
        "${SCRIPT_DIR}/prune-backups.py" \
        "${SCRIPT_DIR}/prune-observability.py" \
        "${SCRIPT_DIR}/export-metrics.py" \
        "${SCRIPT_DIR}/extract-review-metrics.py" \
        "${SCRIPT_DIR}/ops-report.py" \
        "${SCRIPT_DIR}/status-report.py" \
        "${SCRIPT_DIR}/startup-notify.sh" \
        "${SCRIPT_DIR}/api-retry-loop.sh" \
        "${SCRIPT_DIR}/../runtime/backup-personal-state.py" \
        "${SCRIPT_DIR}/../runtime/manage-workspace-agents.py"; do
        install_managed_file "$asset" "/usr/local/lib/hermes-ops/$(basename "$asset")" 0755 root root
    done
    # Cron only imports reviews; the systemd timer owns export and snapshots.
    install_review_metrics_sync
    ln -sfn /usr/local/lib/hermes-ops/ops-report.py /usr/local/bin/hermes-ops-report

    # This file contains only repository-owned non-secret policy. Re-render it
    # on every deployment so stale values cannot override vps-defaults.yml.
    render "${SCRIPT_DIR}/templates/hermes-ops.conf" /etc/hermes-ops.conf 0644
    render "${SCRIPT_DIR}/templates/hermes-ops.logrotate" /etc/logrotate.d/hermes-ops 0644

    install -d -o root -g root -m 0755 "/etc/systemd/system/${gateway_service}.d"
    render \
        "${SCRIPT_DIR}/systemd/hermes-gateway-observability.conf" \
        "/etc/systemd/system/${gateway_service}.d/observability.conf" 0644 root "$gateway_service"

    log "installing Prometheus configuration and Grafana dashboards"
    install -d -o root -g prometheus -m 0750 /etc/hermes-observability /etc/hermes-observability/rules
    install -d -o prometheus -g prometheus -m 0750 /var/lib/hermes-prometheus
    install -d -o root -g grafana -m 0750 \
        /etc/grafana/provisioning/datasources \
        /etc/grafana/provisioning/dashboards/hermes
    # The exporter (Hermes user) writes the SQLite snapshot here; Grafana reads it.
    # The exporter runs as Hermes while Grafana reads the snapshot.  Set the
    # setgid bit so atomically replaced snapshots inherit the Grafana group;
    # otherwise SQLite reports the misleading error 14 "out of memory" when
    # the Grafana plugin cannot read the database.
    install -d -o "${HERMES_USER}" -g grafana -m 2750 /var/lib/hermes-observability
    if [[ -e /var/lib/hermes-observability/metrics.db ]]; then
        chown "${HERMES_USER}:grafana" /var/lib/hermes-observability/metrics.db
        chmod 0640 /var/lib/hermes-observability/metrics.db
    fi
    render \
        "${SCRIPT_DIR}/templates/hermes-prometheus.yml" /etc/hermes-observability/prometheus.yml 0640 prometheus hermes-prometheus.service
    install_managed_file \
        "${SCRIPT_DIR}/../observability/rules/hermes.rules.yml" /etc/hermes-observability/rules/hermes.rules.yml 0640 root prometheus hermes-prometheus.service
    render \
        "${SCRIPT_DIR}/templates/grafana-hermes-prometheus.yml" /etc/grafana/provisioning/datasources/hermes-prometheus.yml 0640 grafana grafana-server.service
    install_managed_file \
        "${SCRIPT_DIR}/../observability/grafana/provisioning/datasources/sqlite.yml" /etc/grafana/provisioning/datasources/hermes-sqlite.yml 0640 root grafana grafana-server.service
    install_managed_file \
        "${SCRIPT_DIR}/../observability/grafana/provisioning/dashboards/dashboards.yml" /etc/grafana/provisioning/dashboards/dashboards.yml 0640 root grafana grafana-server.service
    install_managed_file \
        "${SCRIPT_DIR}/../observability/grafana/provisioning/dashboards/hermes/hermes-overview.json" /etc/grafana/provisioning/dashboards/hermes/hermes-overview.json 0640 root grafana grafana-server.service

    for unit in \
        hermes-dashboard.service \
        hermes-node-exporter.service hermes-prometheus.service \
        hermes-startup-notify.service \
        hermes-backup.service hermes-backup.timer \
        hermes-health.service hermes-health.timer \
        hermes-metrics.service hermes-metrics.timer \
        hermes-observability-prune.service hermes-observability-prune.timer; do
        # oneshot services pick up their new definition on the next invocation;
        # restarting backup/health jobs here would run them during deployment.
        local restart_unit=""
        case "$unit" in
            *.timer|hermes-dashboard.service|hermes-node-exporter.service|hermes-prometheus.service)
                restart_unit="$unit";;
            *)
                restart_unit="";;
        esac
        render "${SCRIPT_DIR}/systemd/${unit}" "/etc/systemd/system/${unit}" 0644 root "$restart_unit"
    done

    return
}

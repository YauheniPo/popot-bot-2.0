# shellcheck shell=bash

install_operations_assets() {
    local gateway_service
    gateway_service="$(managed_services gateway)"
    log "installing health checks, metrics exporter, and reporting CLI"
    install -d -o root -g root -m 0755 /usr/local/lib/hermes-ops
    install -o root -g root -m 0755 \
        "${SCRIPT_DIR}/health-check.sh" \
        "${SCRIPT_DIR}/backup.sh" \
        "${SCRIPT_DIR}/prune-backups.py" \
        "${SCRIPT_DIR}/prune-observability.py" \
        "${SCRIPT_DIR}/export-metrics.py" \
        "${SCRIPT_DIR}/ops-report.py" \
        "${SCRIPT_DIR}/status-report.py" \
        "${SCRIPT_DIR}/startup-notify.sh" \
        /usr/local/lib/hermes-ops/
    ln -sfn /usr/local/lib/hermes-ops/ops-report.py /usr/local/bin/hermes-ops-report

    # This file contains only repository-owned non-secret policy. Re-render it
    # on every deployment so stale values cannot override vps-defaults.yml.
    render "${SCRIPT_DIR}/templates/hermes-ops.conf" /etc/hermes-ops.conf 0644
    render "${SCRIPT_DIR}/templates/hermes-ops.logrotate" /etc/logrotate.d/hermes-ops 0644

    install -d -o root -g root -m 0755 "/etc/systemd/system/${gateway_service}.d"
    render \
        "${SCRIPT_DIR}/systemd/hermes-gateway-observability.conf" \
        "/etc/systemd/system/${gateway_service}.d/observability.conf" 0644

    log "installing Prometheus configuration and Grafana dashboards"
    install -d -o root -g prometheus -m 0750 /etc/hermes-observability
    install -d -o prometheus -g prometheus -m 0750 /var/lib/hermes-prometheus
    install -d -o root -g grafana -m 0750 \
        /etc/grafana/provisioning/datasources \
        /etc/grafana/provisioning/dashboards/hermes
    render \
        "${SCRIPT_DIR}/templates/hermes-prometheus.yml" /etc/hermes-observability/prometheus.yml 0640
    chown root:prometheus /etc/hermes-observability/prometheus.yml
    chmod 0640 /etc/hermes-observability/prometheus.yml
    render \
        "${SCRIPT_DIR}/templates/grafana-hermes-prometheus.yml" /etc/grafana/provisioning/datasources/hermes-prometheus.yml 0640
    chown root:grafana /etc/grafana/provisioning/datasources/hermes-prometheus.yml
    chmod 0640 /etc/grafana/provisioning/datasources/hermes-prometheus.yml
    install -o root -g grafana -m 0640 \
        "${SCRIPT_DIR}/../observability/grafana/provisioning/dashboards/dashboards.yml" /etc/grafana/provisioning/dashboards/dashboards.yml
    install -o root -g grafana -m 0640 \
        "${SCRIPT_DIR}/../observability/grafana/provisioning/dashboards/hermes/hermes-overview.json" /etc/grafana/provisioning/dashboards/hermes/hermes-overview.json

    for unit in \
        hermes-dashboard.service \
        hermes-node-exporter.service hermes-prometheus.service \
        hermes-startup-notify.service \
        hermes-backup.service hermes-backup.timer \
        hermes-health.service hermes-health.timer \
        hermes-metrics.service hermes-metrics.timer \
        hermes-observability-prune.service hermes-observability-prune.timer; do
        render "${SCRIPT_DIR}/systemd/${unit}" "/etc/systemd/system/${unit}" 0644
    done

}

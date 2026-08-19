#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HERMES_USER="hermes"
USER_HOME="/home/hermes"
HERMES_HOME="/home/hermes/.hermes"
HERMES_BIN="/home/hermes/.local/bin/hermes"
START_TIMERS=true

log() {
    printf '[hermes-ops] %s\n' "$*"
}

die() {
    printf '[hermes-ops] ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: sudo ./ops/install-ops.sh [options]

Options:
  --user NAME          Hermes system user (default: hermes)
  --user-home PATH     User home (default: /home/hermes)
  --hermes-home PATH   Hermes state directory (default: USER_HOME/.hermes)
  --hermes-bin PATH    Hermes CLI path (default: USER_HOME/.local/bin/hermes)
  --defer-timers       Install but do not start the monitoring timers yet
  -h, --help           Show this help
EOF
}

while (($#)); do
    case "$1" in
        --user)
            (($# >= 2)) || die "--user requires a value"
            HERMES_USER="$2"
            shift 2
            ;;
        --user-home)
            (($# >= 2)) || die "--user-home requires a value"
            USER_HOME="$2"
            shift 2
            ;;
        --hermes-home)
            (($# >= 2)) || die "--hermes-home requires a value"
            HERMES_HOME="$2"
            shift 2
            ;;
        --hermes-bin)
            (($# >= 2)) || die "--hermes-bin requires a value"
            HERMES_BIN="$2"
            shift 2
            ;;
        --defer-timers)
            START_TIMERS=false
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

[[ "${EUID}" -eq 0 ]] || die "run this installer as root"
[[ "${HERMES_USER}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || die "invalid user name"
id "${HERMES_USER}" >/dev/null 2>&1 || die "user does not exist: ${HERMES_USER}"
for path in "${USER_HOME}" "${HERMES_HOME}" "${HERMES_BIN}"; do
    [[ "${path}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || die "unsafe or unsupported path: ${path}"
done

HERMES_GROUP="$(id -gn "${HERMES_USER}")"

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

enable_observability_plugin() {
    local hermes_python="${HERMES_HOME}/hermes-agent/venv/bin/python"
    [[ -x "${hermes_python}" ]] || {
        log "WARNING: Hermes Python environment is unavailable; the observability plugin was not enabled"
        return 0
    }

    # `hermes plugins enable` can block indefinitely when it is run from a
    # non-interactive provisioner. Hermes documents plugins.enabled in
    # config.yaml as the equivalent explicit opt-in, so update that small
    # allow-list directly instead.
    runuser -u "${HERMES_USER}" -- env -i \
        HOME="${USER_HOME}" \
        HERMES_HOME="${HERMES_HOME}" \
        PATH="${USER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin" \
        "${hermes_python}" - "${HERMES_HOME}/config.yaml" <<'PY'
from pathlib import Path
import os
import sys

import yaml

path = Path(sys.argv[1])
data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
data = data or {}
if not isinstance(data, dict):
    raise SystemExit("Hermes config.yaml must contain a YAML mapping")

plugins = data.setdefault("plugins", {})
if not isinstance(plugins, dict):
    raise SystemExit("Hermes config.yaml plugins must be a YAML mapping")

enabled = plugins.setdefault("enabled", [])
if not isinstance(enabled, list) or not all(isinstance(item, str) for item in enabled):
    raise SystemExit("Hermes config.yaml plugins.enabled must be a list of strings")

if "ops-observability" not in enabled:
    enabled.append("ops-observability")

temporary = path.with_name(f".{path.name}.ops.tmp")
temporary.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
}

install_observability_dependencies() {
    command -v apt-get >/dev/null 2>&1 || die "Grafana observability requires apt-get on Debian/Ubuntu"
    command -v curl >/dev/null 2>&1 || die "curl is required to install the official Grafana package repository"

    log "installing Prometheus, node exporter, and Grafana"
    install -d -o root -g root -m 0755 /etc/apt/keyrings
    # `openssl` is needed only for the direct-deploy fallback password. Install
    # it before creating that file; Ansible normally creates the file from Vault.
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssl
    # Prepare Grafana's password and unit override before package installation:
    # the package manager may otherwise start Grafana with its default admin
    # account before our first-run credentials are visible.
    install_grafana_password_file
    install -d -o root -g root -m 0755 /etc/systemd/system/grafana-server.service.d
    install -o root -g root -m 0644 \
        "${SCRIPT_DIR}/systemd/grafana-hermes-loopback.conf" /etc/systemd/system/grafana-server.service.d/hermes-loopback.conf
    systemctl daemon-reload
    curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
        https://apt.grafana.com/gpg-full.key --output /etc/apt/keyrings/grafana.asc
    chmod 0644 /etc/apt/keyrings/grafana.asc
    printf '%s\n' 'deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.grafana.com stable main' \
        >/etc/apt/sources.list.d/grafana.list
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        grafana prometheus prometheus-node-exporter

    chown root:root /etc/hermes-grafana.env
    chmod 0600 /etc/hermes-grafana.env

    # Distribution package services can bind an address other than loopback.
    # Hermes replaces them with the hardened units below.
    systemctl disable --now prometheus.service prometheus-node-exporter.service 2>/dev/null || true
}

install_grafana_password_file() {
    if [[ -f /etc/hermes-grafana.env ]]; then
        log "preserving existing Grafana administrator credentials"
        return
    fi
    command -v openssl >/dev/null 2>&1 || die "openssl is required to create the Grafana administrator password"
    umask 077
    printf 'GF_SECURITY_ADMIN_USER=hermes\nGF_SECURITY_ADMIN_PASSWORD=%s\n' \
        "$(openssl rand -base64 36 | tr -d '\n')" >/etc/hermes-grafana.env
    chown root:root /etc/hermes-grafana.env
    chmod 0600 /etc/hermes-grafana.env
    log "created Grafana administrator credentials in /etc/hermes-grafana.env (password not printed)"
}

install_observability_dependencies

log "installing the privacy-aware observability plugin"
install -d -o "${HERMES_USER}" -g "${HERMES_GROUP}" -m 0700 \
    "${HERMES_HOME}/plugins/ops-observability" \
    "${HERMES_HOME}/plugins/ops-observability/dashboard/dist" \
    "${HERMES_HOME}/ops/metrics" \
    "${HERMES_HOME}/logs" \
    "${HERMES_HOME}/state-snapshots" \
    "${USER_HOME}/hermes-backups"
# `install -d` does not reliably correct ownership of an already-existing
# parent directory.  These paths must remain private to the Hermes service
# account, including after a previous root-run maintenance command.
chown "${HERMES_USER}:${HERMES_GROUP}" \
    "${HERMES_HOME}/ops" \
    "${HERMES_HOME}/ops/metrics" \
    "${HERMES_HOME}/logs" \
    "${HERMES_HOME}/state-snapshots" \
    "${USER_HOME}/hermes-backups"
chmod 0700 \
    "${HERMES_HOME}/ops" \
    "${HERMES_HOME}/ops/metrics" \
    "${HERMES_HOME}/logs" \
    "${HERMES_HOME}/state-snapshots" \
    "${USER_HOME}/hermes-backups"
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
install -o "${HERMES_USER}" -g "${HERMES_GROUP}" -m 0600 \
    "${SCRIPT_DIR}/plugin/ops-observability/plugin.yaml" \
    "${SCRIPT_DIR}/plugin/ops-observability/__init__.py" \
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

log "installing health checks, metrics exporter, and reporting CLI"
install -d -o root -g root -m 0755 /usr/local/lib/hermes-ops
install -o root -g root -m 0755 \
    "${SCRIPT_DIR}/health-check.sh" \
    "${SCRIPT_DIR}/backup.sh" \
    "${SCRIPT_DIR}/export-metrics.py" \
    "${SCRIPT_DIR}/ops-report.py" \
    /usr/local/lib/hermes-ops/
ln -sfn /usr/local/lib/hermes-ops/ops-report.py /usr/local/bin/hermes-ops-report

if [[ ! -e /etc/hermes-ops.conf ]]; then
    render "${SCRIPT_DIR}/templates/hermes-ops.conf" /etc/hermes-ops.conf 0644
else
    log "preserving existing /etc/hermes-ops.conf"
fi
render "${SCRIPT_DIR}/templates/hermes-ops.logrotate" /etc/logrotate.d/hermes-ops 0644

log "installing Prometheus configuration and Grafana dashboards"
install -d -o root -g prometheus -m 0750 /etc/hermes-observability
install -d -o prometheus -g prometheus -m 0750 /var/lib/hermes-prometheus
install -d -o root -g grafana -m 0750 \
    /etc/grafana/provisioning/datasources \
    /etc/grafana/provisioning/dashboards/hermes
install -o root -g prometheus -m 0640 \
    "${SCRIPT_DIR}/templates/hermes-prometheus.yml" /etc/hermes-observability/prometheus.yml
install -o root -g grafana -m 0640 \
    "${SCRIPT_DIR}/templates/grafana-hermes-prometheus.yml" /etc/grafana/provisioning/datasources/hermes-prometheus.yml
install -o root -g grafana -m 0640 \
    "${SCRIPT_DIR}/../observability/grafana/provisioning/dashboards/dashboards.yml" /etc/grafana/provisioning/dashboards/dashboards.yml
install -o root -g grafana -m 0640 \
    "${SCRIPT_DIR}/../observability/grafana/provisioning/dashboards/hermes/hermes-overview.json" /etc/grafana/provisioning/dashboards/hermes/hermes-overview.json

for unit in \
    hermes-dashboard.service \
    hermes-node-exporter.service hermes-prometheus.service \
    hermes-backup.service hermes-backup.timer \
    hermes-health.service hermes-health.timer \
    hermes-metrics.service hermes-metrics.timer; do
    render "${SCRIPT_DIR}/systemd/${unit}" "/etc/systemd/system/${unit}" 0644
done

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

systemctl daemon-reload
log "enabling the loopback-only Prometheus and Grafana services"
systemctl enable --now hermes-node-exporter.service hermes-prometheus.service grafana-server.service
log "enabling the loopback-only Hermes dashboard"
systemctl enable --now hermes-dashboard.service
systemctl restart hermes-dashboard.service
if [[ "${START_TIMERS}" == "true" ]]; then
    log "creating and verifying the initial scheduled backup"
    systemctl start hermes-backup.service
    systemctl enable --now hermes-backup.timer hermes-health.timer hermes-metrics.timer
else
    log "timers installed but deferred"
fi

if systemctl is-active --quiet hermes-gateway.service; then
    log "restarting the gateway once so it loads the plugin"
    systemctl restart hermes-gateway.service
fi

log "installed successfully"
log "report: sudo -u ${HERMES_USER} HERMES_HOME=${HERMES_HOME} hermes-ops-report --period 24h"
log "timers: systemctl list-timers 'hermes-*'"
log "Grafana: http://127.0.0.1:3000 (credentials are in /etc/hermes-grafana.env)"

#!/usr/bin/env bash

set -Eeuo pipefail

readonly HERMES_USER="${HERMES_USER:-hermes}"
readonly HERMES_HOME="${HERMES_HOME:-/home/${HERMES_USER}/.hermes}"
readonly DASHBOARD_PORT="${HERMES_DASHBOARD_PORT:-9119}"
readonly GRAFANA_PORT="${HERMES_GRAFANA_PORT:-3000}"
readonly PROMETHEUS_PORT="${HERMES_PROMETHEUS_PORT:-9090}"
readonly CODE_SERVER_PORT="${HERMES_CODE_SERVER_PORT:-3001}"
readonly SEARXNG_PORT="${HERMES_SEARXNG_PORT:-8888}"

log() {
    printf '[hermes-tailscale] %s\n' "$*"
}

die() {
    printf '[hermes-tailscale] ERROR: %s\n' "$*" >&2
    exit 1
}

[[ "${EUID}" -eq 0 ]] || die "run this script as root"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v systemctl >/dev/null 2>&1 || die "systemd is required"
id "${HERMES_USER}" >/dev/null 2>&1 || die "Hermes user does not exist: ${HERMES_USER}"

if ! command -v tailscale >/dev/null 2>&1; then
    log "Installing Tailscale from the official installer"
    curl --fail --silent --show-error --location https://tailscale.com/install.sh | sh
fi

systemctl enable --now tailscaled.service

if ! tailscale ip -4 >/dev/null 2>&1; then
    log "Starting interactive Tailscale login with Tailscale SSH enabled"
    tailscale up --ssh
fi

TAILSCALE_IP="$(tailscale ip -4)"
[[ "${TAILSCALE_IP}" =~ ^100\.[0-9.]+$ ]] || die "Tailscale did not return a private IPv4 address"
SHORT_HOSTNAME="$(hostname -s)"
TAILSCALE_HOSTNAME="$(tailscale status --json | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data.get("Self", {}).get("DNSName", "").rstrip("."))')"
[[ "${TAILSCALE_HOSTNAME}" == *.* ]] || TAILSCALE_HOSTNAME="${SHORT_HOSTNAME}.ts.net"
PUBLIC_URL="https://${TAILSCALE_HOSTNAME}"

HERMES_CLI="${HERMES_HOME%/.hermes}/.local/bin/hermes"
HERMES_ENV_FILE="${HERMES_HOME}/.env"
if [[ ! -s "${HERMES_ENV_FILE}" ]] ||
    ! grep -q '^HERMES_DASHBOARD_OAUTH_CLIENT_ID=' "${HERMES_ENV_FILE}" ||
    ! grep -q "^HERMES_DASHBOARD_PUBLIC_URL=${PUBLIC_URL}$" "${HERMES_ENV_FILE}"; then
    if [[ -x "${HERMES_CLI}" ]] && [[ -t 0 ]]; then
        log "Registering Hermes Dashboard OAuth client for ${TAILSCALE_HOSTNAME}"
        runuser --user "${HERMES_USER}" -- env HOME="${HERMES_HOME%/.hermes}" HERMES_HOME="${HERMES_HOME}" \
            "${HERMES_CLI}" dashboard register \
            --name "${SHORT_HOSTNAME}" \
            --redirect-uri "${PUBLIC_URL}/auth/callback"
    else
        log "Dashboard OAuth registration was skipped; run hermes dashboard register with --redirect-uri ${PUBLIC_URL}/auth/callback"
    fi
fi

# Hermes validates the Host header. Keep the dashboard loopback-bound, but
# explicitly trust the MagicDNS hostname used by Tailscale Serve.
CONFIG_FILE="${HERMES_HOME}/config.yaml"
install -d -o "${HERMES_USER}" -g "${HERMES_USER}" -m 0700 "${HERMES_HOME}"
dashboard_auth_ready=false
if [[ -f "${HERMES_ENV_FILE}" ]] && grep -q '^HERMES_DASHBOARD_OAUTH_CLIENT_ID=' "${HERMES_ENV_FILE}"; then
    dashboard_auth_ready=true
fi
if [[ -f "${CONFIG_FILE}" ]] && [[ "${dashboard_auth_ready}" == true ]]; then
    CONFIG_FILE="${CONFIG_FILE}" PUBLIC_URL="${PUBLIC_URL}" python3 - <<'PY'
from pathlib import Path
import os
import re

try:
    import yaml
except ImportError as exc:
    raise SystemExit(f"PyYAML is required to persist dashboard settings: {exc}")

path = Path(os.environ["CONFIG_FILE"])
public_url = os.environ["PUBLIC_URL"]
env_text = Path(os.environ["CONFIG_FILE"]).with_name(".env").read_text(encoding="utf-8")
match = re.search(r"^HERMES_DASHBOARD_OAUTH_CLIENT_ID=(.*)$", env_text, re.MULTILINE)
if not match:
    raise SystemExit("dashboard OAuth client ID is missing")
client_id = match.group(1).strip().strip('"')
data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
dashboard = data.setdefault("dashboard", {})
dashboard["public_url"] = public_url
dashboard.setdefault("oauth", {})["client_id"] = client_id
path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
PY
    chown "${HERMES_USER}:${HERMES_USER}" "${CONFIG_FILE}"
    chmod 0600 "${CONFIG_FILE}"
else
    log "Dashboard public URL was not configured because OAuth registration is incomplete"
fi

log "Configuring private Tailscale Serve endpoints"
tailscale serve reset >/dev/null 2>&1 || true
tailscale serve --bg --https=443 "http://127.0.0.1:${DASHBOARD_PORT}"
tailscale serve --bg --https="${GRAFANA_PORT}" "http://127.0.0.1:${GRAFANA_PORT}"
tailscale serve --bg --https="${PROMETHEUS_PORT}" "http://127.0.0.1:${PROMETHEUS_PORT}"
tailscale serve --bg --https="${CODE_SERVER_PORT}" "http://127.0.0.1:${CODE_SERVER_PORT}"
tailscale serve --bg --https="${SEARXNG_PORT}" "http://127.0.0.1:${SEARXNG_PORT}"

if systemctl list-unit-files hermes-dashboard.service >/dev/null 2>&1; then
    systemctl try-restart hermes-dashboard.service || true
fi

log "Tailscale IP: ${TAILSCALE_IP}"
log "Open Hermes Dashboard: ${PUBLIC_URL}/"
log "Open Grafana: https://${TAILSCALE_HOSTNAME}:${GRAFANA_PORT}"
log "Open Prometheus: https://${TAILSCALE_HOSTNAME}:${PROMETHEUS_PORT}"
log "Open code-server: https://${TAILSCALE_HOSTNAME}:${CODE_SERVER_PORT}"
log "Open SearXNG: https://${TAILSCALE_HOSTNAME}:${SEARXNG_PORT}"
log "SSH: ssh ${HERMES_USER}@${TAILSCALE_IP}"

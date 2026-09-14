#!/usr/bin/env bash

set -Eeuo pipefail

readonly HERMES_USER="${HERMES_USER:-hermes}"
readonly HERMES_HOME="${HERMES_HOME:-/home/${HERMES_USER}/.hermes}"
readonly OPS_CONFIG="${HERMES_OPS_CONFIG:-/etc/hermes-ops.conf}"

# Serve endpoints are rendered by Ansible from vps_tailscale.serve.services into
# the managed ops config, so this bootstrap shares a single source of truth with
# the playbook task. The built-in list is only a fallback for a host where the
# managed config is not present yet.
DEFAULT_SERVE_ENDPOINTS="$(cat <<'ENDPOINTS'
https 443 http://127.0.0.1:9119
https 3000 http://127.0.0.1:3000
https 9090 http://127.0.0.1:9090
https 3001 http://127.0.0.1:3001
https 8888 http://127.0.0.1:8888
ENDPOINTS
)"
readonly DEFAULT_SERVE_ENDPOINTS

log() {
    printf '[hermes-tailscale] %s\n' "$*"
}

die() {
    printf '[hermes-tailscale] ERROR: %s\n' "$*" >&2
    exit 1
}

serve_endpoints() {
    # Prefer the Ansible-rendered list; fall back to the built-in defaults.
    if [[ -r "${OPS_CONFIG}" ]]; then
        local rendered
        rendered="$(awk -F= '/^HERMES_TAILSCALE_SERVE_ENDPOINTS=/ { print substr($0, length($1) + 2); exit }' "${OPS_CONFIG}")"
        if [[ -n "${rendered}" ]]; then
            rendered="$(printf '%s' "${rendered}" | tr ',' '\n' | tr -s ' ')"
            printf '%s\n' "${rendered}"
            return 0
        fi
    fi
    printf '%s\n' "${DEFAULT_SERVE_ENDPOINTS}"
}

[[ "${EUID}" -eq 0 ]] || die "run this script as root"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v systemctl >/dev/null 2>&1 || die "systemd is required"
id "${HERMES_USER}" >/dev/null 2>&1 || die "Hermes user does not exist: ${HERMES_USER}"

if ! command -v tailscale >/dev/null 2>&1; then
    log "Installing Tailscale from the official installer"
    # Pin the transfer and every redirect to HTTPS so --location cannot follow a
    # downgrade to a clear-text URL before the script is executed.
    curl --fail --silent --show-error --location \
        --proto '=https' --proto-redir '=https' \
        https://tailscale.com/install.sh | sh
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
    ! grep -qFx "HERMES_DASHBOARD_PUBLIC_URL=${PUBLIC_URL}" "${HERMES_ENV_FILE}"; then
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
import tempfile

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
# Replace atomically so an interrupted write cannot leave a truncated
# config.yaml that would stop Hermes from starting.
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=path.parent,
    prefix=f".{path.name}.", suffix=".tmp", delete=False,
) as stream:
    temporary = Path(stream.name)
    yaml.safe_dump(data, stream, sort_keys=False, allow_unicode=True)
os.replace(temporary, path)
PY
    chown "${HERMES_USER}:${HERMES_USER}" "${CONFIG_FILE}"
    chmod 0600 "${CONFIG_FILE}"
else
    log "Dashboard public URL was not configured because OAuth registration is incomplete"
fi

log "Configuring private Tailscale Serve endpoints"

# Derive "protocol port target" triples from the single source of truth, then only reset
# and republish when the live Serve state differs. A second run with no config
# change therefore leaves the endpoints untouched.
declare -a desired_pairs=()
while IFS= read -r line; do
    [[ -n "${line// /}" ]] || continue
    protocol="${line%% *}"
    remaining="${line#* }"
    port="${remaining%% *}"
    target="${remaining#* }"
    [[ "${protocol}" == "http" || "${protocol}" == "https" ]] || die "invalid Tailscale Serve protocol: ${protocol}"
    [[ "${port}" =~ ^[1-9][0-9]{0,4}$ ]] || die "invalid Tailscale Serve port: ${port}"
    [[ "${target}" =~ ^http://127[.]0[.]0[.]1:[1-9][0-9]{0,4}$ ]] || die "invalid Tailscale Serve target: ${target}"
    desired_pairs+=("${protocol}|${port}|${target}")
done < <(serve_endpoints)
[[ "${#desired_pairs[@]}" -gt 0 ]] || die "no Tailscale Serve endpoints are configured"

current_pairs="$(tailscale serve status --json 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
pairs = []
tcp = data.get("TCP") or {}
for host, entry in (data.get("Web") or {}).items():
    proxy = ((entry.get("Handlers") or {}).get("/") or {}).get("Proxy")
    port = host.rsplit(":", 1)[-1]
    listener = tcp.get(port) or {}
    protocol = "https" if listener.get("HTTPS") else "http" if listener.get("HTTP") else ""
    if proxy and protocol:
        pairs.append(protocol + "|" + port + "|" + proxy)
print("\n".join(sorted(pairs)))
' || true)"

desired_sorted="$(printf '%s\n' "${desired_pairs[@]}" | sort)"
current_sorted="$(printf '%s\n' "${current_pairs}" | sed '/^$/d' | sort)"
if [[ "${current_sorted}" == "${desired_sorted}" ]]; then
    log "Serve endpoints already match the configured set; nothing to change"
else
    # Reset is expected to succeed once Tailscale is connected; surface a
    # failure instead of hiding it, but keep going so stale endpoints are
    # replaced by the desired set below.
    if ! tailscale serve reset >/dev/null 2>&1; then
        log "WARNING: tailscale serve reset reported an error; recreating endpoints anyway"
    fi
    for pair in "${desired_pairs[@]}"; do
        protocol="${pair%%|*}"
        remaining="${pair#*|}"
        port="${remaining%%|*}"
        target="${pair##*|}"
        tailscale serve --bg "--${protocol}=${port}" "${target}"
    done
fi

if systemctl list-unit-files hermes-dashboard.service >/dev/null 2>&1; then
    systemctl try-restart hermes-dashboard.service || true
fi

log "Tailscale IP: ${TAILSCALE_IP}"
log "Open Hermes Dashboard: ${PUBLIC_URL}/"
for pair in "${desired_pairs[@]}"; do
    protocol="${pair%%|*}"
    remaining="${pair#*|}"
    port="${remaining%%|*}"
    target="${pair##*|}"
    if [[ "${port}" == "443" ]]; then
        continue
    fi
    log "Open ${protocol}://${TAILSCALE_HOSTNAME}:${port} -> ${target}"
done
log "SSH: ssh ${HERMES_USER}@${TAILSCALE_IP}"

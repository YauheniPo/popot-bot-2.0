#!/usr/bin/env bash
set -Eeuo pipefail

HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
HERMES_BIN="${HERMES_BIN:-${HOME}/.local/bin/hermes}"
HERMES_ALERT_TARGET="${HERMES_ALERT_TARGET:-telegram}"
HERMES_GATEWAY_MAINTENANCE_FILE="${HERMES_HOME}/ops/gateway-maintenance"
HERMES_GATEWAY_MAINTENANCE_MAX_AGE_SECONDS="${HERMES_GATEWAY_MAINTENANCE_MAX_AGE_SECONDS:-900}"

gateway_maintenance_active() {
    local modified_epoch now_epoch age_seconds
    [[ -f "${HERMES_GATEWAY_MAINTENANCE_FILE}" ]] || return 1
    modified_epoch="$(stat -c '%Y' "${HERMES_GATEWAY_MAINTENANCE_FILE}" 2>/dev/null || stat -f '%m' "${HERMES_GATEWAY_MAINTENANCE_FILE}" 2>/dev/null || true)"
    [[ "${modified_epoch}" =~ ^[0-9]+$ ]] || return 1
    now_epoch="$(date +%s)"
    age_seconds=$((now_epoch - modified_epoch))
    ((age_seconds >= 0 && age_seconds <= HERMES_GATEWAY_MAINTENANCE_MAX_AGE_SECONDS))
}

if gateway_maintenance_active; then
    logger -t hermes-startup-notify "Gateway startup notification suppressed during planned deployment maintenance"
    exit 0
fi

if [[ ! -x "${HERMES_BIN}" ]]; then
    logger -t hermes-startup-notify "Hermes CLI is not executable: ${HERMES_BIN}"
    exit 0
fi

model="$("${HERMES_BIN}" config get model.default 2>/dev/null | tr -d '\r\n' || true)"
if [[ ! "${model}" =~ ^[A-Za-z0-9_.:/-]{1,200}$ ]]; then
    model="unavailable"
fi

message="Hermes gateway запущен
VPS: $(hostname -f 2>/dev/null || hostname)
Модель по умолчанию: ${model}
Время: $(date --iso-8601=seconds)"

for attempt in 1 2 3; do
    if "${HERMES_BIN}" send --to "${HERMES_ALERT_TARGET}" "${message}"; then
        exit 0
    fi
    if ((attempt < 3)); then
        sleep 2
    fi
done

logger -t hermes-startup-notify "Failed to send gateway startup notification to ${HERMES_ALERT_TARGET}"

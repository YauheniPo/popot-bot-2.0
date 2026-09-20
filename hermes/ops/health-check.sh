#!/usr/bin/env bash
set -Eeuo pipefail

HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
HERMES_BIN="${HERMES_BIN:-${HOME}/.local/bin/hermes}"
HERMES_ALERT_TARGET="${HERMES_ALERT_TARGET:-telegram}"
HERMES_GATEWAY_SERVICE="${HERMES_GATEWAY_SERVICE:-hermes-gateway.service}"
HERMES_DISK_PATH="${HERMES_DISK_PATH:-${HERMES_HOME}}"
HERMES_DISK_WARN_PERCENT="${HERMES_DISK_WARN_PERCENT:-85}"
HERMES_INODE_WARN_PERCENT="${HERMES_INODE_WARN_PERCENT:-85}"
HERMES_MEMORY_AVAILABLE_WARN_PERCENT="${HERMES_MEMORY_AVAILABLE_WARN_PERCENT:-10}"
HERMES_LOAD_WARN_PER_CPU="${HERMES_LOAD_WARN_PER_CPU:-2}"
HERMES_BACKUP_REQUIRED="${HERMES_BACKUP_REQUIRED:-false}"
HERMES_BACKUP_DIR="${HERMES_BACKUP_DIR:-${HOME}/hermes-backups}"
HERMES_BACKUP_MAX_AGE_HOURS="${HERMES_BACKUP_MAX_AGE_HOURS:-26}"
HERMES_FULL_BACKUP_MAX_AGE_HOURS="${HERMES_FULL_BACKUP_MAX_AGE_HOURS:-192}"
HERMES_METRICS_FILE="${HERMES_METRICS_FILE:-${HERMES_HOME}/ops/metrics/hermes.prom}"
HERMES_METRICS_MAX_AGE_MINUTES="${HERMES_METRICS_MAX_AGE_MINUTES:-5}"
HERMES_SEARXNG_URL="${HERMES_SEARXNG_URL:-}"
HERMES_SEARXNG_TIMEOUT_SECONDS="${HERMES_SEARXNG_TIMEOUT_SECONDS:-5}"
HERMES_GATEWAY_MAINTENANCE_FILE="${HERMES_HOME}/ops/gateway-maintenance"
HERMES_GATEWAY_MAINTENANCE_MAX_AGE_SECONDS="${HERMES_GATEWAY_MAINTENANCE_MAX_AGE_SECONDS:-900}"

STATE_DIR="${HERMES_HOME}/ops"
STATE_FILE="${STATE_DIR}/health-state"
mkdir -p "${STATE_DIR}"
chmod 700 "${STATE_DIR}"

declare -a issue_keys=()
declare -a issue_messages=()

add_issue() {
    issue_keys+=("$1")
    issue_messages+=("$2")
}

is_number() {
    local value="$1"
    [[ "${value}" =~ ^[0-9]+([.][0-9]+)?$ ]]
}

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
    logger -t hermes-health "Health notification suppressed during planned deployment maintenance"
    exit 0
fi

for threshold in \
    "${HERMES_DISK_WARN_PERCENT}" \
    "${HERMES_INODE_WARN_PERCENT}" \
    "${HERMES_MEMORY_AVAILABLE_WARN_PERCENT}" \
    "${HERMES_LOAD_WARN_PER_CPU}" \
    "${HERMES_BACKUP_MAX_AGE_HOURS}" \
    "${HERMES_FULL_BACKUP_MAX_AGE_HOURS}" \
    "${HERMES_METRICS_MAX_AGE_MINUTES}"; do
    if ! is_number "${threshold}"; then
        add_issue "configuration" "Некорректный числовой порог в /etc/hermes-ops.conf"
        break
    fi
done

if ! systemctl is-active --quiet "${HERMES_GATEWAY_SERVICE}"; then
    add_issue "gateway" "gateway ${HERMES_GATEWAY_SERVICE} не запущен"
fi

if [[ -n "${HERMES_SEARXNG_URL}" ]]; then
    if ! [[ "${HERMES_SEARXNG_URL}" =~ ^https?://[^[:space:]]+$ ]] ||
       ! is_number "${HERMES_SEARXNG_TIMEOUT_SECONDS}"; then
        add_issue "searxng-configuration" "Некорректный URL или timeout SearXNG"
    elif ! command -v curl >/dev/null 2>&1; then
        add_issue "searxng-health" "нельзя проверить SearXNG: curl не установлен"
    elif ! curl --fail --silent --show-error --max-time "${HERMES_SEARXNG_TIMEOUT_SECONDS}" \
        --get --data-urlencode 'q=hermes' --data-urlencode 'format=json' \
        "${HERMES_SEARXNG_URL%/}/search" >/dev/null 2>&1; then
        add_issue "searxng-health" "SearXNG не отвечает: ${HERMES_SEARXNG_URL}"
    fi
fi

if disk_line="$(df -P "${HERMES_DISK_PATH}" 2>/dev/null | awk 'NR==2 {gsub(/%/, "", $5); print $5}')" && is_number "${disk_line}"; then
    if awk -v used="${disk_line}" -v limit="${HERMES_DISK_WARN_PERCENT}" 'BEGIN {exit !(used >= limit)}'; then
        add_issue "disk" "диск занят на ${disk_line}% (порог ${HERMES_DISK_WARN_PERCENT}%)"
    fi
else
    add_issue "disk-check" "не удалось проверить диск ${HERMES_DISK_PATH}"
fi

if inode_line="$(df -Pi "${HERMES_DISK_PATH}" 2>/dev/null | awk 'NR==2 {gsub(/%/, "", $5); print $5}')" \
    && is_number "${inode_line}" \
    && awk -v used="${inode_line}" -v limit="${HERMES_INODE_WARN_PERCENT}" 'BEGIN {exit !(used >= limit)}'; then
    add_issue "inodes" "inode заняты на ${inode_line}% (порог ${HERMES_INODE_WARN_PERCENT}%)"
fi

memory_total="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || true)"
memory_available="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null || true)"
if is_number "${memory_total}" && is_number "${memory_available}" && [[ "${memory_total}" != "0" ]]; then
    memory_percent="$(awk -v available="${memory_available}" -v total="${memory_total}" 'BEGIN {printf "%.1f", available * 100 / total}')"
    if awk -v available="${memory_percent}" -v limit="${HERMES_MEMORY_AVAILABLE_WARN_PERCENT}" 'BEGIN {exit !(available <= limit)}'; then
        add_issue "memory" "доступно ${memory_percent}% RAM (порог ${HERMES_MEMORY_AVAILABLE_WARN_PERCENT}%)"
    fi
else
    add_issue "memory-check" "не удалось проверить память"
fi

cpu_count="$(getconf _NPROCESSORS_ONLN 2>/dev/null || true)"
load_one="$(awk '{print $1}' /proc/loadavg 2>/dev/null || true)"
if is_number "${cpu_count}" && is_number "${load_one}" && [[ "${cpu_count}" != "0" ]] &&
    is_number "${HERMES_LOAD_WARN_PER_CPU}" &&
    load_limit="$(awk -v cpus="${cpu_count}" -v factor="${HERMES_LOAD_WARN_PER_CPU}" 'BEGIN {printf "%.1f", cpus * factor}')" &&
    is_number "${load_limit}"; then
    if awk -v load1="${load_one}" -v limit="${load_limit}" 'BEGIN {exit !(load1 >= limit)}'; then
        add_issue "load" "load1=${load_one}, порог ${load_limit} для ${cpu_count} CPU"
    else
        load_check_rc=$?
        # 1 means below threshold; an awk/runtime failure is not a healthy host.
        if ((load_check_rc != 1)); then
            add_issue "load-check" "ошибка проверки нагрузки (awk exit ${load_check_rc})"
        fi
    fi
else
    add_issue "load-check" "не удалось проверить нагрузку или вычислить её порог"
fi

if [[ -f "${HERMES_METRICS_FILE}" ]]; then
    metrics_created="$(stat -c '%Y' "${HERMES_METRICS_FILE}" 2>/dev/null || true)"
    if is_number "${metrics_created}"; then
        metrics_age_minutes="$(awk -v now="$(date +%s)" -v created="${metrics_created}" 'BEGIN {printf "%.1f", (now-created)/60}')"
        if awk -v age="${metrics_age_minutes}" -v limit="${HERMES_METRICS_MAX_AGE_MINUTES}" 'BEGIN {exit !(age >= limit)}'; then
            add_issue "metrics" "метрики не обновлялись ${metrics_age_minutes} мин (порог ${HERMES_METRICS_MAX_AGE_MINUTES} мин)"
        fi
    fi
else
    add_issue "metrics" "файл метрик не найден: ${HERMES_METRICS_FILE}"
fi

if [[ "${HERMES_BACKUP_REQUIRED}" == "true" ]]; then
    newest_archive=""
    if [[ -d "${HERMES_BACKUP_DIR}" ]]; then
        newest_archive="$(find "${HERMES_BACKUP_DIR}" -maxdepth 1 -type f ! -name '*.partial.zip' -printf '%T@\n' 2>/dev/null | sort -nr | head -n1 || true)"
    fi
    newest_snapshot="$(find "${HERMES_HOME}/state-snapshots" -mindepth 1 -maxdepth 1 -type d -printf '%T@\n' 2>/dev/null | sort -nr | head -n1 || true)"
    newest_backup="$(printf '%s\n%s\n' "${newest_archive}" "${newest_snapshot}" | awk 'NF { if ($1 > newest) newest = $1 } END { if (newest) print newest }')"
    if ! is_number "${newest_backup}"; then
        add_issue "backup" "backup не найден в ${HERMES_BACKUP_DIR} или ${HERMES_HOME}/state-snapshots"
    else
        now_epoch="$(date +%s)"
        backup_age_hours="$(awk -v now="${now_epoch}" -v created="${newest_backup}" 'BEGIN {printf "%.1f", (now-created)/3600}')"
        if awk -v age="${backup_age_hours}" -v limit="${HERMES_BACKUP_MAX_AGE_HOURS}" 'BEGIN {exit !(age >= limit)}'; then
            add_issue "backup" "последнему backup ${backup_age_hours} ч (порог ${HERMES_BACKUP_MAX_AGE_HOURS} ч)"
        fi
    fi

    newest_full="$(find "${HERMES_BACKUP_DIR}" -maxdepth 1 -type f -name 'scheduled-full-*.zip' ! -name '*.partial.zip' -printf '%T@\n' 2>/dev/null | sort -nr | head -n1 || true)"
    if ! is_number "${newest_full}"; then
        add_issue "full-backup" "полный scheduled backup не найден"
    else
        full_age_hours="$(awk -v now="$(date +%s)" -v created="${newest_full}" 'BEGIN {printf "%.1f", (now-created)/3600}')"
        if awk -v age="${full_age_hours}" -v limit="${HERMES_FULL_BACKUP_MAX_AGE_HOURS}" 'BEGIN {exit !(age >= limit)}'; then
            add_issue "full-backup" "полному backup ${full_age_hours} ч (порог ${HERMES_FULL_BACKUP_MAX_AGE_HOURS} ч)"
        fi
    fi
fi

current_tmp="$(mktemp "${STATE_DIR}/health-state.XXXXXX")"
trap 'rm -f "${current_tmp}"' EXIT
if ((${#issue_keys[@]} > 0)); then
    printf '%s\n' "${issue_keys[@]}" | sort -u >"${current_tmp}"
else
    : >"${current_tmp}"
fi
chmod 600 "${current_tmp}"

previous=""
if [[ -f "${STATE_FILE}" ]]; then
    previous="$(<"${STATE_FILE}")"
fi
current="$(<"${current_tmp}")"

declare -a new_messages=()
declare -a recovered=()
for index in "${!issue_keys[@]}"; do
    if ! grep -Fqx -- "${issue_keys[$index]}" <<<"${previous}"; then
        new_messages+=("• ${issue_messages[$index]}")
    fi
done
while IFS= read -r old_key; do
    [[ -n "${old_key}" ]] || continue
    if ! grep -Fqx -- "${old_key}" <<<"${current}"; then
        recovered+=("• ${old_key}")
    fi
done <<<"${previous}"

if ((${#new_messages[@]} == 0 && ${#recovered[@]} == 0)); then
    mv -f "${current_tmp}" "${STATE_FILE}"
    trap - EXIT
    exit 0
fi

hostname_text="$(hostname -f 2>/dev/null || hostname)"
message="Hermes VPS: ${hostname_text}"
if ((${#new_messages[@]} > 0)); then
    message+=$'\n\nПроблемы:'
    for item in "${new_messages[@]}"; do
        message+=$'\n'"${item}"
    done
fi
if ((${#recovered[@]} > 0)); then
    message+=$'\n\nВосстановлено:'
    for item in "${recovered[@]}"; do
        message+=$'\n'"${item}"
    done
fi

if [[ ! -x "${HERMES_BIN}" ]]; then
    logger -t hermes-health "Hermes CLI is not executable: ${HERMES_BIN}"
    exit 1
fi

if ! "${HERMES_BIN}" send --to "${HERMES_ALERT_TARGET}" "${message}"; then
    logger -t hermes-health "Failed to send alert to ${HERMES_ALERT_TARGET}"
    exit 1
fi

mv -f "${current_tmp}" "${STATE_FILE}"
trap - EXIT

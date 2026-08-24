#!/usr/bin/env bash
set -Eeuo pipefail

HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
HERMES_BIN="${HERMES_BIN:-${HOME}/.local/bin/hermes}"
HERMES_RUN_AS_USER="${HERMES_RUN_AS_USER:-}"
HERMES_USER_HOME="${HERMES_USER_HOME:-${HOME}}"
HERMES_BACKUP_DIR="${HERMES_BACKUP_DIR:-${HERMES_USER_HOME}/hermes-backups}"
HERMES_BACKUP_RETENTION_DAYS="${HERMES_BACKUP_RETENTION_DAYS:-14}"
HERMES_FULL_BACKUP_DAY="${HERMES_FULL_BACKUP_DAY:-7}"
HERMES_FULL_BACKUP_RETENTION_DAYS="${HERMES_FULL_BACKUP_RETENTION_DAYS:-35}"

if [[ ! "${HERMES_BACKUP_RETENTION_DAYS}" =~ ^[1-9][0-9]*$ ]] ||
    [[ ! "${HERMES_FULL_BACKUP_RETENTION_DAYS}" =~ ^[1-9][0-9]*$ ]] ||
    [[ ! "${HERMES_FULL_BACKUP_DAY}" =~ ^[1-7]$ ]]; then
    printf 'Invalid backup retention or weekday setting\n' >&2
    exit 2
fi
[[ -x "${HERMES_BIN}" ]] || {
    printf 'Hermes CLI is not executable: %s\n' "${HERMES_BIN}" >&2
    exit 2
}

mkdir -p "${HERMES_BACKUP_DIR}" "${HERMES_HOME}/ops"
chmod 700 "${HERMES_BACKUP_DIR}" "${HERMES_HOME}/ops"

exec 9>"${HERMES_HOME}/ops/backup.lock"
flock -n 9 || exit 0

timestamp="$(date -u +%Y%m%d-%H%M%S)"
backup_mode="quick"
if [[ "$(date -u +%u)" == "${HERMES_FULL_BACKUP_DAY}" ]] ||
    ! find "${HERMES_BACKUP_DIR}" -maxdepth 1 -type f -name 'scheduled-full-*.zip' ! -name '*.partial.zip' -print -quit | grep -q .; then
    backup_mode="full"
fi
declare -a backup_args=(backup)
if [[ "${backup_mode}" == "quick" ]]; then
    backup_args+=(--quick --label scheduled)
else
    backup_file="${HERMES_BACKUP_DIR}/scheduled-full-${timestamp}.zip"
    temporary_file="${backup_file%.zip}.partial.zip"
    trap 'rm -f -- "${temporary_file}"' EXIT
    backup_args+=(--output "${temporary_file}")
fi

if [[ "${EUID}" -eq 0 && -n "${HERMES_RUN_AS_USER}" ]]; then
    /usr/sbin/runuser --user "${HERMES_RUN_AS_USER}" -- env -i \
        HOME="${HERMES_USER_HOME}" \
        HERMES_HOME="${HERMES_HOME}" \
        LANG="${LANG:-C.UTF-8}" \
        LOGNAME="${HERMES_RUN_AS_USER}" \
        PATH="${HERMES_USER_HOME}/.local/bin:${HERMES_HOME}/node/bin:/usr/local/bin:/usr/bin:/bin" \
        USER="${HERMES_RUN_AS_USER}" \
        "${HERMES_BIN}" "${backup_args[@]}"
else
    "${HERMES_BIN}" "${backup_args[@]}"
fi

# Hermes --quick creates a consistent state snapshot in state-snapshots rather
# than a zip at --output.  Only the scheduled full run has an archive to move.
if [[ "${backup_mode}" == "full" ]]; then
    # Move first so a chmod failure can never destroy a completed backup.
    # With set -e a failed mv exits with the conventional 1 and the EXIT
    # trap removes the .partial file; the next scheduled run recreates it.
    mv -f "${temporary_file}" "${backup_file}"
    chmod 600 "${backup_file}"
    trap - EXIT
fi

# Retention is deliberately limited to the dedicated backup directory.
find "${HERMES_BACKUP_DIR}" -maxdepth 1 -type f \
    -name 'scheduled-quick-*.zip' -mtime "+${HERMES_BACKUP_RETENTION_DAYS}" -delete
find "${HERMES_BACKUP_DIR}" -maxdepth 1 -type f \
    -name 'scheduled-full-*.zip' -mtime "+${HERMES_FULL_BACKUP_RETENTION_DAYS}" -delete

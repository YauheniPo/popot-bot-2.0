#!/usr/bin/env bash
set -Eeuo pipefail

HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
HERMES_BIN="${HERMES_BIN:-${HOME}/.local/bin/hermes}"
HERMES_RUN_AS_USER="${HERMES_RUN_AS_USER:-}"
HERMES_USER_HOME="${HERMES_USER_HOME:-${HOME}}"
HERMES_WORKSPACE="${HERMES_WORKSPACE:-${HERMES_USER_HOME}/workspace}"
HERMES_BACKUP_DIR="${HERMES_BACKUP_DIR:-${HERMES_USER_HOME}/hermes-backups}"
HERMES_BACKUP_RETENTION_DAYS="${HERMES_BACKUP_RETENTION_DAYS:-14}"
HERMES_FULL_BACKUP_DAY="${HERMES_FULL_BACKUP_DAY:-7}"
HERMES_FULL_BACKUP_KEEP="${HERMES_FULL_BACKUP_KEEP:-5}"
HERMES_DEPLOYMENT_BACKUP_KEEP="${HERMES_DEPLOYMENT_BACKUP_KEEP:-10}"
HERMES_BACKUP_PRUNER="${HERMES_BACKUP_PRUNER:-$(dirname "${BASH_SOURCE[0]}")/prune-backups.py}"

if [[ ! "${HERMES_BACKUP_RETENTION_DAYS}" =~ ^[1-9][0-9]*$ ]] ||
    [[ ! "${HERMES_FULL_BACKUP_KEEP}" =~ ^[1-9][0-9]*$ ]] ||
    [[ ! "${HERMES_DEPLOYMENT_BACKUP_KEEP}" =~ ^[1-9][0-9]*$ ]] ||
    [[ ! "${HERMES_FULL_BACKUP_DAY}" =~ ^[1-7]$ ]]; then
    printf 'Invalid backup retention or weekday setting\n' >&2
    exit 2
fi
[[ -x "${HERMES_BIN}" ]] || {
    printf 'Hermes CLI is not executable: %s\n' "${HERMES_BIN}" >&2
    exit 2
}
[[ -f "${HERMES_BACKUP_PRUNER}" ]] || {
    printf 'Hermes backup retention helper is missing: %s\n' "${HERMES_BACKUP_PRUNER}" >&2
    exit 2
}
[[ "${HERMES_WORKSPACE}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || {
    printf 'Hermes workspace must be a non-empty absolute safe path: %s\n' "${HERMES_WORKSPACE}" >&2
    exit 2
}
if [[ "${EUID}" -eq 0 && -n "${HERMES_RUN_AS_USER}" ]]; then
    id "${HERMES_RUN_AS_USER}" >/dev/null 2>&1 || {
        printf 'Hermes backup run-as user does not exist: %s\n' "${HERMES_RUN_AS_USER}" >&2
        exit 2
    }
fi

mkdir -p "${HERMES_BACKUP_DIR}" "${HERMES_HOME}/ops"
chmod 700 "${HERMES_BACKUP_DIR}" "${HERMES_HOME}/ops"

exec 9>"${HERMES_HOME}/ops/backup.lock"
flock -n 9 || exit 0

# workspace/AGENTS.md contains the operator's personal agent instructions but
# lives outside HERMES_HOME. Mirror it into the full-backup tree before every
# scheduled run so weekly archives can restore it on a replacement VPS.
workspace_agents="${HERMES_WORKSPACE}/AGENTS.md"
operator_state_dir="${HERMES_HOME}/operator-state"
operator_state_agents="${operator_state_dir}/workspace-AGENTS.md"
if [[ -f "${workspace_agents}" ]]; then
    if [[ "${EUID}" -eq 0 && -n "${HERMES_RUN_AS_USER}" ]]; then
        install -d -o "${HERMES_RUN_AS_USER}" -m 0700 \
            "${operator_state_dir}"
        install -o "${HERMES_RUN_AS_USER}" -m 0600 \
            "${workspace_agents}" "${operator_state_agents}"
    else
        mkdir -p "${operator_state_dir}"
        chmod 700 "${operator_state_dir}"
        install -m 0600 "${workspace_agents}" "${operator_state_agents}"
    fi
elif [[ -e "${operator_state_agents}" ]]; then
    # Do not let a full backup restore instructions that the operator has
    # deliberately removed from the workspace since the previous run.
    rm -f -- "${operator_state_agents}"
    printf 'Removed stale workspace AGENTS.md mirror from backup state\n' >&2
fi

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

# Retention only removes service-created quick snapshots and full archives.
python3 "${HERMES_BACKUP_PRUNER}" \
    --backup-dir "${HERMES_BACKUP_DIR}" \
    --snapshots-dir "${HERMES_HOME}/state-snapshots" \
    --quick-retention-days "${HERMES_BACKUP_RETENTION_DAYS}" \
    --full-keep "${HERMES_FULL_BACKUP_KEEP}" \
    --deployment-keep "${HERMES_DEPLOYMENT_BACKUP_KEEP}"

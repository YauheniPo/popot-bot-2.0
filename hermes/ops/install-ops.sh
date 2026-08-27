#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HERMES_USER="hermes"
USER_HOME="/home/hermes"
HERMES_HOME="/home/hermes/.hermes"
HERMES_BIN="/home/hermes/.local/bin/hermes"
WORKSPACE_DIR="/home/hermes/workspace"
BACKUP_DIR="/home/hermes/hermes-backups"
START_TIMERS=true
RESTART_SERVICES=true
PLUGIN_CONTENT_CHANGED=false
PLUGIN_CONFIGURATION_CHANGED=false
VSCODE_ENABLED=false
VSCODE_COMPOSE_FILE=""
VSCODE_ENV_FILE="/etc/code-server.env"
VSCODE_PROJECT_NAME=""

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
  --workspace PATH     Agent workspace (default: USER_HOME/workspace)
  --backup-dir PATH    Full-backup directory (default: USER_HOME/hermes-backups)
  --vscode-enabled     Register the managed code-server restart command
  --vscode-compose-file PATH
                       Managed code-server Compose file
  --vscode-env-file PATH
                       Root-only Compose environment (default: /etc/code-server.env)
  --vscode-project-name NAME
                       Managed Docker Compose project name
  --defer-timers       Install but do not start the monitoring timers yet
  --no-restart         Let the parent deploy restart managed runtime services
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
        --backup-dir)
            (($# >= 2)) || die "--backup-dir requires a value"
            BACKUP_DIR="$2"
            shift 2
            ;;
        --workspace)
            (($# >= 2)) || die "--workspace requires a value"
            WORKSPACE_DIR="$2"
            shift 2
            ;;
        --vscode-enabled)
            VSCODE_ENABLED=true
            shift
            ;;
        --vscode-compose-file)
            (($# >= 2)) || die "--vscode-compose-file requires a value"
            VSCODE_COMPOSE_FILE="$2"
            shift 2
            ;;
        --vscode-env-file)
            (($# >= 2)) || die "--vscode-env-file requires a value"
            VSCODE_ENV_FILE="$2"
            shift 2
            ;;
        --vscode-project-name)
            (($# >= 2)) || die "--vscode-project-name requires a value"
            VSCODE_PROJECT_NAME="$2"
            shift 2
            ;;
        --defer-timers)
            START_TIMERS=false
            shift
            ;;
        --no-restart)
            RESTART_SERVICES=false
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
for path in "${USER_HOME}" "${HERMES_HOME}" "${HERMES_BIN}" "${WORKSPACE_DIR}" "${BACKUP_DIR}"; do
    [[ "${path}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || die "unsafe or unsupported path: ${path}"
done
if [[ "${VSCODE_ENABLED}" == true ]]; then
    [[ -n "${VSCODE_COMPOSE_FILE}" ]] ||
        die "--vscode-enabled requires --vscode-compose-file"
    [[ "${VSCODE_PROJECT_NAME}" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] ||
        die "--vscode-enabled requires a safe --vscode-project-name"
    for path in "${VSCODE_COMPOSE_FILE}" "${VSCODE_ENV_FILE}"; do
        [[ "${path}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] ||
            die "unsafe or unsupported code-server path: ${path}"
    done
fi
# The CLI path is later executed in a privileged context. An attacker with
# write access to any parent directory could plant a symlink that redirects
# that execution, so reject a link both at the CLI path itself and at every
# parent component of the absolute path.
[[ ! -L "${HERMES_BIN}" ]] ||
    die "HERMES_BIN must not be a symlink: ${HERMES_BIN}"
path_component="$(dirname -- "${HERMES_BIN}")"
while [[ "${path_component}" != "/" ]]; do
    [[ ! -L "${path_component}" ]] ||
        die "HERMES_BIN path must not contain a symlink component: ${path_component}"
    path_component="$(dirname -- "${path_component}")"
done

HERMES_GROUP="$(id -gn "${HERMES_USER}")"

# Installation domains share the validated context above.
# shellcheck source=install/common.sh
source "$SCRIPT_DIR/install/common.sh"
# shellcheck source=install/packages.sh
source "$SCRIPT_DIR/install/packages.sh"
# shellcheck source=install/plugin.sh
source "$SCRIPT_DIR/install/plugin.sh"
# shellcheck source=install/assets.sh
source "$SCRIPT_DIR/install/assets.sh"
# shellcheck source=install/services.sh
source "$SCRIPT_DIR/install/services.sh"

install_observability_dependencies
install_observability_plugin
install_operations_assets
reconcile_observability_services

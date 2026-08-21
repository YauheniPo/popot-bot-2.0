#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HERMES_USER="hermes"
USER_HOME="/home/hermes"
HERMES_HOME="/home/hermes/.hermes"
HERMES_BIN="/home/hermes/.local/bin/hermes"
START_TIMERS=true
RESTART_SERVICES=true
PLUGIN_CONTENT_CHANGED=false
PLUGIN_CONFIGURATION_CHANGED=false

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
for path in "${USER_HOME}" "${HERMES_HOME}" "${HERMES_BIN}"; do
    [[ "${path}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || die "unsafe or unsupported path: ${path}"
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

#!/usr/bin/env bash
set -Eeuo pipefail

HERMES_USER="hermes"
USER_HOME="/home/hermes"
HERMES_HOME="/home/hermes/.hermes"
AGENT_BROWSER_VERSION=""
AGENT_BROWSER_ARGS=""

log() {
    printf '[hermes-browser] %s\n' "$*"
}

die() {
    printf '[hermes-browser] ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: sudo ./ops/install-browser-automation.sh [options]

Install and verify the local agent-browser runtime and its dedicated Chrome for
Testing build. This is used by Hermes for interactive public web pages.

Options:
  --user NAME          Hermes system user (default: hermes)
  --user-home PATH     User home (default: /home/hermes)
  --hermes-home PATH   Hermes state directory (default: USER_HOME/.hermes)
  --version VERSION    Exact agent-browser version from vps-defaults.yml
  --launch-args ARGS   Comma-separated Chrome launch arguments
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
        --version)
            (($# >= 2)) || die "--version requires a value"
            AGENT_BROWSER_VERSION="$2"
            shift 2
            ;;
        --launch-args)
            (($# >= 2)) || die "--launch-args requires a value"
            AGENT_BROWSER_ARGS="$2"
            shift 2
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

SETTINGS_FILE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../config" && pwd)/vps-defaults.yml"
CONFIG_APPLIER="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../runtime" && pwd)/apply-config.py"
if [[ -z "${AGENT_BROWSER_VERSION}" ]]; then
    AGENT_BROWSER_VERSION="$(python3 "${CONFIG_APPLIER}" value \
        --settings "${SETTINGS_FILE}" vps_browser.agent_browser_version)"
fi
if [[ -z "${AGENT_BROWSER_ARGS}" ]]; then
    AGENT_BROWSER_ARGS="$(python3 "${CONFIG_APPLIER}" value \
        --settings "${SETTINGS_FILE}" vps_browser.launch_args)"
fi

[[ "${EUID}" -eq 0 ]] || die "run this installer as root"
[[ "${HERMES_USER}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || die "invalid user name"
id "${HERMES_USER}" >/dev/null 2>&1 || die "user does not exist"
for path in "${USER_HOME}" "${HERMES_HOME}"; do
    [[ "${path}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || die "unsafe or unsupported path: ${path}"
done
[[ "${AGENT_BROWSER_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
    die "invalid agent-browser version"
[[ "${AGENT_BROWSER_ARGS}" =~ ^--[A-Za-z0-9=,_-]+$ ]] ||
    die "invalid agent-browser launch arguments"

NODE_BIN="${HERMES_HOME}/node/bin"
NPM_BIN="${NODE_BIN}/npm"
AGENT_BROWSER_BIN="${NODE_BIN}/agent-browser"
BROWSER_CONFIG="${HERMES_HOME}/agent-browser-config.json"
[[ -x "${NPM_BIN}" ]] || die "Hermes-managed npm was not found at ${NPM_BIN}"

run_as_hermes() {
    # The single-quoted script is intentionally expanded by the child Bash.
    # shellcheck disable=SC2016
    runuser --user "${HERMES_USER}" -- env -i \
        HOME="${USER_HOME}" \
        HERMES_HOME="${HERMES_HOME}" \
        LANG="${LANG:-C.UTF-8}" \
        LOGNAME="${HERMES_USER}" \
        PATH="${USER_HOME}/.local/bin:${NODE_BIN}:/usr/local/bin:/usr/bin:/bin" \
        AGENT_BROWSER_ARGS="${AGENT_BROWSER_ARGS}" \
        AGENT_BROWSER_CONFIG="${BROWSER_CONFIG}" \
        SHELL=/bin/bash \
        USER="${HERMES_USER}" \
        /bin/bash -c 'cd -- "$1"; shift; exec "$@"' bash "${USER_HOME}" "$@" </dev/null
}

installed_browser_version="$(python3 -c \
    'import json, pathlib, sys; path=pathlib.Path(sys.argv[1]); print(json.loads(path.read_text())["version"]) if path.is_file() else None' \
    "${HERMES_HOME}/node/lib/node_modules/agent-browser/package.json" 2>/dev/null || true)"
if [[ "${installed_browser_version}" != "${AGENT_BROWSER_VERSION}" ]]; then
    log "installing agent-browser ${AGENT_BROWSER_VERSION}"
    run_as_hermes "${NPM_BIN}" install --global --omit=dev \
        --allow-scripts=agent-browser --prefix "${HERMES_HOME}/node" \
        "agent-browser@${AGENT_BROWSER_VERSION}"
fi

log "writing persistent local browser launch configuration"
# The single-quoted script is intentionally expanded by the child Bash.
# shellcheck disable=SC2016
run_as_hermes /bin/bash -c '
    set -Eeuo pipefail
    config_path="$1"
    launch_args="$2"
    umask 077
    printf "{\n  \"args\": \"%s\"\n}\n" "${launch_args}" >"${config_path}"
    chmod 600 "${config_path}"
' bash "${BROWSER_CONFIG}" "${AGENT_BROWSER_ARGS}"

log "installing or verifying the dedicated Chrome for Testing build"
run_as_hermes "${AGENT_BROWSER_BIN}" --config "${BROWSER_CONFIG}" install

log "running a live local browser launch check"
# Some agent-browser versions do not reliably apply launch configuration
# to its own probe. An open/snapshot/close cycle validates the actual daemon and
# Chrome path Hermes uses instead.
run_as_hermes "${AGENT_BROWSER_BIN}" --config "${BROWSER_CONFIG}" open about:blank
run_as_hermes "${AGENT_BROWSER_BIN}" --config "${BROWSER_CONFIG}" snapshot
run_as_hermes "${AGENT_BROWSER_BIN}" --config "${BROWSER_CONFIG}" close

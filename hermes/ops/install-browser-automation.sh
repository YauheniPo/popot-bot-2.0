#!/usr/bin/env bash
set -Eeuo pipefail

HERMES_USER="hermes"
USER_HOME="/home/hermes"
HERMES_HOME="/home/hermes/.hermes"
# Ubuntu 23.10+ commonly blocks Chromium's user-namespace sandbox through
# AppArmor on VPS images. Keep the workaround explicit and identical to the
# value written to Hermes's managed environment file by Ansible.
AGENT_BROWSER_ARGS="--no-sandbox,--disable-dev-shm-usage"

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
id "${HERMES_USER}" >/dev/null 2>&1 || die "user does not exist"
for path in "${USER_HOME}" "${HERMES_HOME}"; do
    [[ "${path}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || die "unsafe or unsupported path: ${path}"
done

NODE_BIN="${HERMES_HOME}/node/bin"
NPM_BIN="${NODE_BIN}/npm"
AGENT_BROWSER_BIN="${NODE_BIN}/agent-browser"
BROWSER_CONFIG="${HERMES_HOME}/agent-browser-config.json"
[[ -x "${NPM_BIN}" ]] || die "Hermes-managed npm was not found at ${NPM_BIN}"

run_as_hermes() {
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

if [[ ! -x "${AGENT_BROWSER_BIN}" ]]; then
    log "installing agent-browser"
    run_as_hermes "${NPM_BIN}" install --global --omit=dev \
        --allow-scripts=agent-browser --prefix "${HERMES_HOME}/node" agent-browser
fi

log "writing persistent local browser launch configuration"
run_as_hermes /bin/bash -c '
    set -Eeuo pipefail
    config_path="$1"
    umask 077
    cat > "${config_path}" <<JSON
{
  "args": "--no-sandbox,--disable-dev-shm-usage"
}
JSON
    chmod 600 "${config_path}"
' bash "${BROWSER_CONFIG}"

log "installing or verifying the dedicated Chrome for Testing build"
run_as_hermes "${AGENT_BROWSER_BIN}" --config "${BROWSER_CONFIG}" install

log "running a live local browser launch check"
# agent-browser 0.34.x's `doctor` does not reliably apply launch configuration
# to its own probe. An open/snapshot/close cycle validates the actual daemon and
# Chrome path Hermes uses instead.
run_as_hermes "${AGENT_BROWSER_BIN}" --config "${BROWSER_CONFIG}" open about:blank
run_as_hermes "${AGENT_BROWSER_BIN}" --config "${BROWSER_CONFIG}" snapshot
run_as_hermes "${AGENT_BROWSER_BIN}" --config "${BROWSER_CONFIG}" close

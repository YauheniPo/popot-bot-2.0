#!/usr/bin/env bash
# Retry a single Hermes CLI query using the deployment's provider and model.
# Usage: api-retry-loop.sh [MODEL_NAME] ["USER_MESSAGE"]
# The name is retained for installed callers; no dashboard API is required.
set -Eeuo pipefail

OPS_CONFIG="${HERMES_OPS_CONFIG:-/etc/hermes-ops.conf}"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

required_config_value() {
    local key="$1" value
    [[ -r "$OPS_CONFIG" ]] || die "Cannot read ${OPS_CONFIG}; run the managed deployment first"
    value="$(awk -v key="$key" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$OPS_CONFIG")"
    [[ -n "$value" ]] || die "${key} must not be empty in ${OPS_CONFIG}"
    printf '%s' "$value"
}

# Read non-secret policy without sourcing it as shell code. Hermes itself loads
# credentials from its own home; neither the wrapper nor its arguments carry keys.
RUN_AS_USER="$(required_config_value HERMES_RUN_AS_USER)"
HERMES_USER_HOME="$(required_config_value HERMES_USER_HOME)"
HERMES_HOME="$(required_config_value HERMES_HOME)"
HERMES_BIN="$(required_config_value HERMES_BIN)"
PROVIDER_NAME="$(required_config_value HERMES_API_RETRY_PROVIDER)"
MODEL_NAME="${1:-$(required_config_value HERMES_API_RETRY_MODEL)}"
USER_MESSAGE="${2:-$(required_config_value HERMES_API_RETRY_MESSAGE)}"
MAX_ATTEMPTS="$(required_config_value HERMES_API_RETRY_MAX_ATTEMPTS)"
WAIT_SECONDS="$(required_config_value HERMES_API_RETRY_WAIT_SECONDS)"
TIMEOUT_SECONDS="$(required_config_value HERMES_API_RETRY_TIMEOUT_SECONDS)"

[[ "$RUN_AS_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "Invalid Hermes service user"
[[ "$PROVIDER_NAME" =~ ^[a-z][a-z0-9-]*$ ]] || die "Invalid Hermes provider"
[[ "$MODEL_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._:/+-]*$ ]] || die "Invalid model name"
[[ -n "${USER_MESSAGE//[[:space:]]/}" ]] || die "Request message must not be empty"
for path in "$HERMES_USER_HOME" "$HERMES_HOME" "$HERMES_BIN"; do
    [[ "$path" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || die "Invalid Hermes runtime path"
done
[[ -x "$HERMES_BIN" ]] || die "Hermes CLI is not executable: ${HERMES_BIN}"
if ! [[ "$MAX_ATTEMPTS" =~ ^[1-9][0-9]?$ ]]; then
    die "HERMES_API_RETRY_MAX_ATTEMPTS must be between 1 and 99"
fi
for setting in WAIT_SECONDS TIMEOUT_SECONDS; do
    value="${!setting}"
    if ! [[ "$value" =~ ^[1-9][0-9]{0,3}$ ]] || (( value > 3600 )); then
        die "${setting} must be between 1 and 3600"
    fi
done
service_uid="$(id -u "$RUN_AS_USER")" || die "Hermes service user does not exist"
[[ "$service_uid" != 0 ]] || die "Refusing to run model queries as root"
TIMEOUT_BIN="$(command -v timeout)" || die "The coreutils timeout command is required"

runner=()
if [[ "$(id -u)" == 0 ]]; then
    runner=(runuser -u "$RUN_AS_USER" --)
elif [[ "$(id -un)" != "$RUN_AS_USER" ]]; then
    die "Run this helper as ${RUN_AS_USER} or through sudo"
fi
runner+=(env -i HOME="$HERMES_USER_HOME" HERMES_HOME="$HERMES_HOME"
    USER="$RUN_AS_USER" LOGNAME="$RUN_AS_USER" LANG=C.UTF-8
    PATH="$HERMES_USER_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
    "$TIMEOUT_BIN" --kill-after=5s "${TIMEOUT_SECONDS}s"
    "$HERMES_BIN" chat --provider "$PROVIDER_NAME" --model "$MODEL_NAME"
    --quiet --toolsets none --max-turns 1 --query-file -)
cd -- "$HERMES_USER_HOME"

printf 'Hermes query: provider=%s model=%s; up to %s CLI attempts\n' "$PROVIDER_NAME" "$MODEL_NAME" "$MAX_ATTEMPTS"
for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
    # stdin keeps arbitrary messages literal and out of the process arguments.
    # Quiet mode exits after one query. The empty toolset limits this helper to
    # inference, and timeout bounds the whole CLI attempt including its retries.
    if response="$(printf '%s' "$USER_MESSAGE" | "${runner[@]}")"; then
        if [[ -n "${response//[[:space:]]/}" ]]; then
            printf '%s\n' "$response"
            exit 0
        fi
        # A partial Hermes result can exit zero without a usable answer. This
        # helper checks inference, so require text as well as a successful exit.
        status=0
        printf 'Hermes CLI attempt %s/%s returned no answer (exit 0).\n' "$attempt" "$MAX_ATTEMPTS" >&2
    else
        status=$?
        printf 'Hermes CLI attempt %s/%s failed (exit %s).\n' "$attempt" "$MAX_ATTEMPTS" "$status" >&2
    fi
    # Status 2 is Hermes' argparse/usage error. This helper supplies a fixed
    # argument vector, so retrying it cannot change the failure; 126/127 mean
    # the executable cannot be invoked and are likewise permanent here.
    if (( status == 2 || status == 126 || status == 127 )); then
        exit "$status"
    fi
    if (( attempt < MAX_ATTEMPTS )); then
        sleep "$WAIT_SECONDS"
    fi
done
die "Attempts exhausted (${MAX_ATTEMPTS})"

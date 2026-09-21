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

optional_config_value() {
    local key="$1"
    [[ -r "$OPS_CONFIG" ]] || die "Cannot read ${OPS_CONFIG}; run the managed deployment first"
    awk -v key="$key" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$OPS_CONFIG"
}

# Read non-secret policy without sourcing it as shell code. Hermes itself loads
# credentials from its own home; neither the wrapper nor its arguments carry keys.
RUN_AS_USER="$(required_config_value HERMES_RUN_AS_USER)"
HERMES_USER_HOME="$(required_config_value HERMES_USER_HOME)"
HERMES_HOME="$(required_config_value HERMES_HOME)"
HERMES_BIN="$(required_config_value HERMES_BIN)"
PROVIDER_NAME="$(required_config_value HERMES_API_RETRY_PROVIDER)"
MODEL_NAME="${1:-$(required_config_value HERMES_API_RETRY_MODEL)}"
FALLBACK_SPEC="$(optional_config_value HERMES_API_RETRY_FALLBACKS)"
USER_MESSAGE="${2:-$(required_config_value HERMES_API_RETRY_MESSAGE)}"
MAX_ATTEMPTS="$(required_config_value HERMES_API_RETRY_MAX_ATTEMPTS)"
WAIT_SECONDS="$(required_config_value HERMES_API_RETRY_WAIT_SECONDS)"
TIMEOUT_SECONDS="$(required_config_value HERMES_API_RETRY_TIMEOUT_SECONDS)"

[[ "$RUN_AS_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "Invalid Hermes service user"
[[ "$PROVIDER_NAME" =~ ^[a-z][a-z0-9-]*$ ]] || die "Invalid Hermes provider"
[[ "$MODEL_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._:/+-]*$ ]] || die "Invalid model name"
[[ -n "${USER_MESSAGE//[[:space:]]/}" ]] || die "Request message must not be empty"
for path in "$HERMES_USER_HOME" "$HERMES_HOME" "$HERMES_BIN"; do
    [[ "$path" == /* && "$path" != *$'\n'* && "$path" != *$'\r'* ]] || die "Invalid Hermes runtime path"
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
[[ -d "$HERMES_USER_HOME" ]] || die "Hermes user home is not a directory: ${HERMES_USER_HOME}"

fallback_routes=()
if [[ -n "$FALLBACK_SPEC" ]]; then
    IFS=',' read -r -a fallback_routes <<<"$FALLBACK_SPEC"
    for route in "${fallback_routes[@]}"; do
        fallback_provider="${route%%:*}"
        fallback_model="${route#*:}"
        [[ "$fallback_provider" =~ ^[a-z][a-z0-9-]*$ ]] || die "Invalid fallback provider"
        [[ "$fallback_model" =~ ^[A-Za-z0-9][A-Za-z0-9._:/+-]*$ ]] || die "Invalid fallback model"
    done
fi

runner=()
if [[ "$(id -u)" == 0 ]]; then
    runner=(runuser -u "$RUN_AS_USER" --)
elif [[ "$(id -un)" != "$RUN_AS_USER" ]]; then
    die "Run this helper as ${RUN_AS_USER} or through sudo"
fi
runner+=(env -i HOME="$HERMES_USER_HOME" HERMES_HOME="$HERMES_HOME"
    USER="$RUN_AS_USER" LOGNAME="$RUN_AS_USER" LANG=C.UTF-8
    LC_ALL="${LC_ALL:-C.UTF-8}" TERM="${TERM:-}"
    HTTP_PROXY="${HTTP_PROXY:-}" HTTPS_PROXY="${HTTPS_PROXY:-}" ALL_PROXY="${ALL_PROXY:-}"
    http_proxy="${http_proxy:-}" https_proxy="${https_proxy:-}" all_proxy="${all_proxy:-}"
    NO_PROXY="${NO_PROXY:-}" no_proxy="${no_proxy:-}"
    SSL_CERT_FILE="${SSL_CERT_FILE:-}" SSL_CERT_DIR="${SSL_CERT_DIR:-}"
    REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-}" CURL_CA_BUNDLE="${CURL_CA_BUNDLE:-}"
    NODE_EXTRA_CA_CERTS="${NODE_EXTRA_CA_CERTS:-}"
    PATH="$HERMES_USER_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
    "$TIMEOUT_BIN" --kill-after=5s "${TIMEOUT_SECONDS}s")
cd -- "$HERMES_USER_HOME" || die "Cannot enter Hermes user home: ${HERMES_USER_HOME}"

record_fallback() {
    # Metadata-only observability row for the Grafana route scorecard. Written
    # as the service user through the same runner; a failure never changes the
    # retry outcome and nothing is created when Hermes has no ops directory yet.
    "${runner[@]}" python3 - "$HERMES_HOME/ops/metrics.db" "$1" "$2" "$3" "$4" <<'PY' || true
import os, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
database = Path(sys.argv[1])
if database.parent.is_dir() and not database.is_symlink():
    connection = sqlite3.connect(database, timeout=3)
    with connection:
        connection.execute("CREATE TABLE IF NOT EXISTS route_fallbacks (ts TEXT NOT NULL, from_provider TEXT,"
                           " from_model TEXT, to_provider TEXT, to_model TEXT)")
        connection.execute("INSERT INTO route_fallbacks VALUES (?, ?, ?, ?, ?)",
                           (datetime.now(timezone.utc).isoformat(timespec="milliseconds"), *sys.argv[2:6]))
    connection.close()
    os.chmod(database, 0o600)
PY
}

printf 'Hermes query: provider=%s model=%s; up to %s CLI attempts\n' "$PROVIDER_NAME" "$MODEL_NAME" "$MAX_ATTEMPTS"
response_file="$(mktemp)" || die "Cannot create a temporary response file"
trap 'rm -f -- "$response_file"' EXIT
fallback_index=0
current_provider="$PROVIDER_NAME"
current_model="$MODEL_NAME"
for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
    # stdin keeps arbitrary messages literal and out of the process arguments.
    # Quiet mode exits after one query. The empty toolset limits this helper to
    # inference, and timeout bounds the whole CLI attempt including its retries.
    runner_for_attempt=("${runner[@]}" "$HERMES_BIN" chat --provider "$current_provider" --model "$current_model"
        --quiet --toolsets none --max-turns 1 --query-file -)
    if printf '%s' "$USER_MESSAGE" | "${runner_for_attempt[@]}" >"$response_file"; then
        if [[ -s "$response_file" ]]; then
            cat "$response_file"
            exit 0
        fi
        # A zero exit with no answer is a deterministic unusable result. Do not
        # spend the full retry budget hiding a configuration or model problem,
        # unless a configured fallback can take over this trigger.
        if (( fallback_index >= ${#fallback_routes[@]} )); then
            die "Hermes CLI returned an empty response (exit 0)"
        fi
        status=1
    else
        status=$?
        printf 'Hermes CLI attempt %s/%s failed (exit %s).\n' "$attempt" "$MAX_ATTEMPTS" "$status" >&2
    fi
    if (( fallback_index < ${#fallback_routes[@]} )); then
        route="${fallback_routes[fallback_index]}"
        previous_provider="$current_provider"
        previous_model="$current_model"
        current_provider="${route%%:*}"
        current_model="${route#*:}"
        ((fallback_index += 1))
        printf 'Switching to fallback provider=%s model=%s for the remaining attempts.\n' \
            "$current_provider" "$current_model" >&2
        record_fallback "$previous_provider" "$previous_model" "$current_provider" "$current_model"
    elif (( status == 2 || status == 126 || status == 127 )); then
        # These are permanent invocation errors only when no fallback route is
        # available for this trigger.
        exit "$status"
    elif (( status == 124 )); then
        printf 'Hermes CLI timed out; no fallback route remains.\n' >&2
        exit "$status"
    fi
    # Once the fallback chain is exhausted, transient provider failures still
    # consume the remaining attempts; permanent invocation errors exited above.
    if (( attempt < MAX_ATTEMPTS )); then
        sleep "$WAIT_SECONDS"
    fi
done
die "Attempts exhausted (${MAX_ATTEMPTS})"

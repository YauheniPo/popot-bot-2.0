#!/usr/bin/env bash
# api-retry-loop.sh — retries model requests after rate limits (429).
# Usage: ./api-retry-loop.sh [MODEL_NAME] ["USER_MESSAGE"]
# Example: ./api-retry-loop.sh "poolside/laguna-s-2.1:free" "write a short description"

set -uo pipefail

OPS_CONFIG="${HERMES_OPS_CONFIG:-/etc/hermes-ops.conf}"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

config_value() {
    local key="$1"
    [[ -r "$OPS_CONFIG" ]] || return 1
    awk -v key="$key" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$OPS_CONFIG"
}

required_config_value() {
    local key="$1"
    local value
    value="$(config_value "$key")" ||
        die "${key} is missing from ${OPS_CONFIG}; run the managed deployment first"
    [[ -n "$value" ]] || die "${key} must not be empty in ${OPS_CONFIG}"
    printf '%s' "$value"
}

# Defaults are rendered from config/vps-defaults.yml into the root-owned ops
# config during deployment. Positional arguments intentionally override only
# the model and request text for one invocation.
API_URL="$(required_config_value HERMES_API_RETRY_ENDPOINT)"
MODEL_NAME="${1:-$(required_config_value HERMES_API_RETRY_MODEL)}"
USER_MESSAGE="${2:-$(required_config_value HERMES_API_RETRY_MESSAGE)}"
MAX_ATTEMPTS="$(required_config_value HERMES_API_RETRY_MAX_ATTEMPTS)"
WAIT_SECONDS="$(required_config_value HERMES_API_RETRY_WAIT_SECONDS)"

[[ "$API_URL" =~ ^http://127\.0\.0\.1:[1-9][0-9]{0,4}/[A-Za-z0-9._~/%:-]*$ ]] ||
    die "HERMES_API_RETRY_ENDPOINT must be a loopback HTTP URL"
[[ "$MODEL_NAME" =~ ^[A-Za-z0-9._:/-]+$ ]] || die "model name contains unsupported characters"
[[ -n "$USER_MESSAGE" ]] || die "request message must not be empty"
if ! [[ "$MAX_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || (( MAX_ATTEMPTS > 99 )); then
    die "HERMES_API_RETRY_MAX_ATTEMPTS must be between 1 and 99"
fi
if ! [[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || (( WAIT_SECONDS > 3600 )); then
    die "HERMES_API_RETRY_WAIT_SECONDS must be between 1 and 3600"
fi

REQUEST_BODY="$(python3 -c '
import json
import sys

print(json.dumps({
    "model": sys.argv[1],
    "messages": [{"role": "user", "content": sys.argv[2]}],
}, ensure_ascii=False))
' "$MODEL_NAME" "$USER_MESSAGE")"

echo "🔄 Starting retry loop for model: $MODEL_NAME"
echo "📡 API endpoint: $API_URL"
echo "⏱ Retry interval: every $WAIT_SECONDS seconds"
echo "========================================"

for ((i = 1; i <= MAX_ATTEMPTS; i++)); do
    # Send the request.
    RESPONSE=$(curl -sS -w "\n%{http_code}" "$API_URL" \
        -H "Content-Type: application/json" \
        --data-binary "$REQUEST_BODY")

    HTTP_CODE="${RESPONSE##*$'\n'}"
    BODY="${RESPONSE%$'\n'*}"

    if [[ "$HTTP_CODE" == "200" ]]; then
        echo "✅ [$(date '+%H:%M:%S')] Success! Response:"
        echo "$BODY"
        exit 0
    elif [[ "$HTTP_CODE" == "429" ]]; then
        REMAINING_MIN=$(( (MAX_ATTEMPTS - i) * WAIT_SECONDS / 60 ))
        echo "🕐 [$(date '+%H:%M:%S')] Rate limited (429). Waiting $WAIT_SECONDS sec... (attempt $i/$MAX_ATTEMPTS, ~${REMAINING_MIN} min remaining)"
        sleep "$WAIT_SECONDS"
    else
        echo "❌ [$(date '+%H:%M:%S')] HTTP $HTTP_CODE: $BODY"
        if [[ $i -lt $MAX_ATTEMPTS ]]; then
            echo "🔄 Waiting $WAIT_SECONDS sec and retrying..."
            sleep "$WAIT_SECONDS"
        fi
    fi
done

echo "🔴 [$(date '+%H:%M:%S')] Attempts exhausted ($MAX_ATTEMPTS). Request failed."
exit 1

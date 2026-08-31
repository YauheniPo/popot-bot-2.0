#!/bin/bash
# notify-doctor.sh - отправка результата hermes doctor в Telegram
# Используется для автоматической проверки состояния системы после обновлений
# или по расписанию через cron/systemd timer.

set -euo pipefail

TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${TELEGRAM_CHAT_ID:-}"

if [[ -z "$TOKEN" || -z "$CHAT_ID" ]]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set" >&2
    exit 1
fi

# Run hermes doctor
if RESULT=$(hermes doctor 2>&1); then
    EXIT_CODE=0
else
    EXIT_CODE=$?
fi

if [[ $EXIT_CODE -ne 0 ]]; then
    MESSAGE=$'❌ Hermes Doctor failed\n'
    MESSAGE+="${RESULT}"
else
    # Truncate output to avoid Telegram message limits (4096 chars)
    if [[ ${#RESULT} -gt 3800 ]]; then
        OUTPUT=$(echo "$RESULT" | head -c 3800)
        OUTPUT="${OUTPUT}... (truncated)"
    else
        OUTPUT="$RESULT"
    fi
    MESSAGE=$'✅ Hermes Doctor\n\n'
    MESSAGE+="${OUTPUT}"
fi

curl --silent --show-error --fail -X POST \
    "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${MESSAGE}" \
    >/dev/null 2>&1 || echo "WARNING: Failed to send Telegram notification" >&2

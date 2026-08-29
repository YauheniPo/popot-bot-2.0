#!/bin/bash
# notify-doctor.sh - отправка результата hermes doctor в Telegram
# Используется для автоматической проверки состояния системы после обновлений
# или по расписанию через cron/systemd timer.

set -euo pipefail

TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${TELEGRAM_CHAT_ID:-}"
MESSAGE="${HERMES_DOCTOR_MESSAGE:-🩺 *Hermes Doctor* — система проверена}"

if [[ -z "$TOKEN" || -z "$CHAT_ID" ]]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set" >&2
    exit 1
fi

# Run hermes doctor
RESULT=$(hermes doctor 2>&1)
EXIT_CODE=$?

if [[ $EXIT_CODE -ne 0 ]]; then
    MESSAGE="❌ *Hermes Doctor failed*\n${RESULT}"
else
    # Truncate output to avoid Telegram message limits (4096 chars)
    if [[ ${#RESULT} -gt 3800 ]]; then
        OUTPUT=$(echo "$RESULT" | head -c 3800)
        OUTPUT="${OUTPUT}... (truncated)"
    else
        OUTPUT="$RESULT"
    fi
    MESSAGE="✅ *Hermes Doctor*\n\n\`\`\`\n${OUTPUT}\n\`\`\`"
fi

curl -s -X POST \
    "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d text="${MESSAGE}" \
    -d parse_mode="MarkdownV2" \
    >/dev/null 2>&1 || echo "WARNING: Failed to send Telegram notification" >&2

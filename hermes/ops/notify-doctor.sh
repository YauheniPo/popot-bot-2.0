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

# Truncate output to avoid Telegram message limits (4096 chars). Applies to
# both branches below: a failed run's output can be just as long as a
# successful one, and it's the one you most need delivered.
if [[ ${#RESULT} -gt 3800 ]]; then
    # iconv -c drops any trailing byte sequence that head -c split mid-character,
    # so the cut always ends on a valid UTF-8 boundary. iconv still exits
    # non-zero for that dropped incomplete tail even with -c (it only
    # suppresses invalid-sequence errors, not incomplete-sequence ones), so
    # `|| true` keeps that expected case from aborting the script under
    # set -e/pipefail.
    OUTPUT=$(echo "$RESULT" | head -c 3800 | iconv -f UTF-8 -t UTF-8 -c 2>/dev/null) || true
    OUTPUT="${OUTPUT}... (truncated)"
else
    OUTPUT="$RESULT"
fi

if [[ $EXIT_CODE -ne 0 ]]; then
    MESSAGE=$'❌ Hermes Doctor failed\n'
    MESSAGE+="${OUTPUT}"
else
    MESSAGE=$'✅ Hermes Doctor\n\n'
    MESSAGE+="${OUTPUT}"
fi

curl --silent --show-error --fail -X POST \
    "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${MESSAGE}" \
    >/dev/null 2>&1 || echo "WARNING: Failed to send Telegram notification" >&2

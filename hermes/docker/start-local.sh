#!/bin/sh
# Select the long-running local Hermes mode without exposing a messenger when
# local.env intentionally has no Telegram credentials. The upstream wrapper
# activates Hermes' virtualenv and drops privileges before calling this script.
set -eu

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_ALLOWED_USERS:-}" ]; then
  echo "[hermes-local] starting Telegram gateway"
  exec hermes gateway run
fi

echo "[hermes-local] Telegram credentials are absent; starting Dashboard only"
# HERMES_DASHBOARD=true makes the upstream s6 service own the Dashboard.
# Keep the image's required main process alive without starting a second web
# server on the same port.
exec sleep infinity

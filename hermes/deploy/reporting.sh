# shellcheck shell=bash

run_diagnostics() {
  log "Checking the Hermes configuration"
  if ! run_as_hermes "$HERMES_BIN" config check; then
    warn "configuration check reported missing or outdated settings"
  fi

  log "Running Hermes diagnostics"
  if ! run_as_hermes "$HERMES_BIN" doctor; then
    warn "Hermes doctor reported items that may still need configuration"
  fi
}

print_summary() {
  local version
  version="$(run_as_hermes "$HERMES_BIN" version 2>&1)" || version="installed (version command failed)"

  printf '\nHermes deployment completed.\n'
  printf '  User:       %s\n' "$HERMES_USER"
  printf '  Home:       %s\n' "$HERMES_HOME"
  printf '  Workspace:  %s\n' "$HERMES_WORKSPACE"
  printf '  Backups:    %s\n' "$HERMES_BACKUP_DIR"
  printf '  Executable: %s\n' "$HERMES_BIN"
  printf '  Version:    %s\n' "$version"

  if [[ "$INSTALL_GOOGLE_CLI" == true ]]; then
    printf '\nConnect Gmail and Google Calendar by asking Hermes:\n'
    printf '  Настрой Google Workspace для Gmail и Calendar\n'
  fi

  if [[ "$INSTALL_DEV_CLIS" == true ]]; then
    printf '\nConnect GitHub and set the commit identity:\n'
    printf '  sudo -u %q -H gh auth login\n' "$HERMES_USER"
    printf '  sudo -u %q -H git config --global user.name "Your Name"\n' "$HERMES_USER"
    printf '  sudo -u %q -H git config --global user.email "you@example.com"\n' "$HERMES_USER"
  fi

  if [[ "$RUN_SETUP" == false ]]; then
    printf '\nConfigure the model provider and tools:\n'
    printf '  sudo -u %q -H %q setup\n' "$HERMES_USER" "$HERMES_BIN"
  fi

  printf '\nConfigure any built-in or custom model provider without putting API keys in shell history:\n'
  printf '  sudo -u %q -H %q model\n' "$HERMES_USER" "$HERMES_BIN"
  printf '  Run it once for each provider or endpoint you want to make available.\n'
  printf '  In chat, use /model to switch between providers already configured.\n'

  if [[ "$ENABLE_GATEWAY" == false ]]; then
    printf '\nTo enable the 24/7 messaging gateway after setup:\n'
    printf '  sudo %q --user %q --enable-gateway\n' "$0" "$HERMES_USER"
  else
    printf '\nGateway logs:\n'
    printf '  sudo journalctl -u hermes-gateway -f\n'
  fi

  if [[ "$INSTALL_TAILSCALE" == true ]]; then
    printf '\nPrivate VPS access with Tailscale:\n'
    local tailscale_ip
    tailscale_ip="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
    if [[ -n "$tailscale_ip" ]]; then
      printf '  Tailscale IP: %s\n' "$tailscale_ip"
      printf '  SSH:          ssh %s@%s\n' "$HERMES_USER" "$tailscale_ip"
    else
      printf '  Finish login: sudo tailscale up --ssh\n'
    fi
  fi

  if [[ "$INSTALL_OPS" == true ]]; then
    printf '\nMonitoring and audit:\n'
    printf '  Telegram: /ops summary 24h  (also: models, tools, costs, commands, health)\n'
    printf '  Report:   sudo -u %q HERMES_HOME=%q hermes-ops-report --period 7d\n' "$HERMES_USER" "$HERMES_HOME"
    printf '  Audit:    %s/logs/ops-audit.jsonl\n' "$HERMES_HOME"
    printf '  Metrics:  %s/ops/metrics/hermes.prom\n' "$HERMES_HOME"
    printf '  Hermes Dashboard: http://127.0.0.1:9119 (via SSH or Tailscale tunnel)\n'
    printf '  Grafana:          http://127.0.0.1:3000 (via SSH or Tailscale tunnel)\n'
    printf '  Grafana password: stored root-only in /etc/hermes-grafana.env\n'
    if [[ "$ENABLE_GATEWAY" == false ]]; then
      printf '  Start:    sudo systemctl enable --now hermes-backup.timer hermes-health.timer hermes-metrics.timer\n'
    fi
  fi

  printf '\nUpdate from Telegram: /update\n'
  printf 'Update from SSH:      sudo -u %q -H %q update\n' "$HERMES_USER" "$HERMES_BIN"

  printf '\nReview enabled tools and web-search routing:\n'
  printf '  sudo -u %q -H %q tools\n' "$HERMES_USER" "$HERMES_BIN"
  printf '  sudo -u %q -H %q status\n' "$HERMES_USER" "$HERMES_BIN"
  if [[ "$RUN_MCP_PICKER" == false ]]; then
    printf '\nReview and install only required Nous-approved MCP integrations:\n'
    printf '  sudo -u %q -H %q mcp\n' "$HERMES_USER" "$HERMES_BIN"
  else
    printf '  sudo -u %q -H %q mcp list\n' "$HERMES_USER" "$HERMES_BIN"
  fi

  printf '\nRun diagnostics:\n'
  printf '  sudo -u %q -H %q doctor\n' "$HERMES_USER" "$HERMES_BIN"
}



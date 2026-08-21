# shellcheck shell=bash

resolve_gateway_choice() {
  [[ "$ENABLE_GATEWAY" == auto ]] || return 0

  if [[ -f "$HERMES_HOME/.env" ]] && \
      grep -q '^TELEGRAM_BOT_TOKEN=' "$HERMES_HOME/.env" && \
      grep -q '^TELEGRAM_ALLOWED_USERS=' "$HERMES_HOME/.env"; then
    ENABLE_GATEWAY=true
    log "Telegram credentials found; enabling the messaging gateway"
  else
    ENABLE_GATEWAY=false
    log "Telegram credentials are absent; deploying without the messaging gateway"
  fi
}

enable_gateway() {
  local user_unit="$HERMES_USER_HOME/.config/systemd/user/hermes-gateway.service"

  command -v systemctl >/dev/null 2>&1 || die "systemd is required for --enable-gateway"
  [[ -d /run/systemd/system ]] || die "systemd is not running on this host"
  [[ ! -e "$user_unit" ]] || die \
    "a user gateway already exists at $user_unit; uninstall it before enabling the system gateway"

  log "Installing and starting the systemd gateway as $HERMES_USER"
  env -i \
    HOME="$HERMES_USER_HOME" \
    HERMES_HOME="$HERMES_HOME" \
    LANG="${LANG:-C.UTF-8}" \
    PATH="$HERMES_USER_HOME/.local/bin:$HERMES_NODE_BIN:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    USER=root \
    LOGNAME=root \
    "$HERMES_BIN" gateway install \
      --system \
      --run-as-user "$HERMES_USER" \
      --force \
      --start-now \
      --start-on-login

  systemctl is-enabled --quiet hermes-gateway.service ||
    die "hermes-gateway.service was not enabled"
  systemctl is-active --quiet hermes-gateway.service || {
    systemctl status --no-pager hermes-gateway.service >&2 || true
    die "hermes-gateway.service did not start"
  }
}

install_operations_layer() {
  [[ "$INSTALL_OPS" == true ]] || return 0
  [[ -x "$SCRIPT_DIR/ops/install-ops.sh" ]] ||
    die "operations installer is missing: $SCRIPT_DIR/ops/install-ops.sh"

  local -a args=(
    --user "$HERMES_USER"
    --user-home "$HERMES_USER_HOME"
    --hermes-home "$HERMES_HOME"
    --hermes-bin "$HERMES_BIN"
    --no-restart
  )
  if [[ "$ENABLE_GATEWAY" == false ]]; then
    args+=(--defer-timers)
  fi

  log "Installing audit logs, usage metrics, and zero-token health alerts"
  "$SCRIPT_DIR/ops/install-ops.sh" "${args[@]}"
}

restart_managed_runtime() {
  local hermes_python="$HERMES_HOME/hermes-agent/venv/bin/python"
  local services_output
  local -a groups=()
  local -a services=()

  if [[ "$ENABLE_GATEWAY" == true ]]; then
    groups+=(gateway)
  fi

  if [[ "$INSTALL_OPS" == true ]]; then
    groups+=(observability)
    if [[ "$ENABLE_GATEWAY" == true ]]; then
      groups+=(timers)
    fi
  fi

  ((${#groups[@]} > 0)) || return 0
  [[ -x "$hermes_python" ]] || die "Hermes Python is missing: $hermes_python"
  services_output="$("$hermes_python" "$VPS_CONFIG_APPLIER" services \
    --settings "$VPS_SETTINGS_FILE" "${groups[@]}")" ||
    die "could not resolve managed Hermes service groups"
  mapfile -t services <<<"$services_output"

  log "Reloading systemd and restarting managed Hermes runtime services"
  systemctl daemon-reload
  systemctl restart "${services[@]}"

  local service
  for service in "${services[@]}"; do
    systemctl is-active --quiet "$service" ||
      die "managed Hermes service did not become active after restart: $service"
  done
}



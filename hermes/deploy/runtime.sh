# shellcheck shell=bash

resolve_user_paths() {
  local passwd_entry
  passwd_entry="$(getent passwd "$HERMES_USER")" ||
    die "cannot read the passwd entry for $HERMES_USER"

  HERMES_USER_HOME="$(cut -d: -f6 <<<"$passwd_entry")"
  [[ "$HERMES_USER_HOME" == /* && "$HERMES_USER_HOME" != "/" ]] ||
    die "unsafe home directory for $HERMES_USER: $HERMES_USER_HOME"
  [[ -d "$HERMES_USER_HOME" ]] || die "home directory does not exist: $HERMES_USER_HOME"
  if [[ -n "$REQUESTED_USER_HOME" && "$HERMES_USER_HOME" != "$REQUESTED_USER_HOME" ]]; then
    die "configured user home $REQUESTED_USER_HOME does not match passwd home $HERMES_USER_HOME"
  fi

  HERMES_HOME="${REQUESTED_HERMES_HOME:-$HERMES_USER_HOME/.hermes}"
  HERMES_INSTALL_DIR="$HERMES_HOME/hermes-agent"
  HERMES_BIN="$HERMES_USER_HOME/.local/bin/hermes"
  HERMES_NODE_BIN="$HERMES_HOME/node/bin"
  HERMES_WORKSPACE="${REQUESTED_HERMES_WORKSPACE:-$HERMES_USER_HOME/workspace}"
  HERMES_BACKUP_DIR="${REQUESTED_HERMES_BACKUP_DIR:-$HERMES_USER_HOME/hermes-backups}"
  HERMES_GROUP="$(id -gn "$HERMES_USER")"

  local managed_path real_home real_managed
  real_home="$(realpath -e "$HERMES_USER_HOME")"
  for managed_path in "$HERMES_HOME" "$HERMES_WORKSPACE" "$HERMES_BACKUP_DIR"; do
    [[ "$managed_path" == "$HERMES_USER_HOME/"* ]] ||
      die "managed path must stay below $HERMES_USER_HOME: $managed_path"
    real_managed="$(realpath -m "$managed_path")"
    [[ "$real_managed" == "$real_home/"* ]] ||
      die "managed path resolves outside $HERMES_USER_HOME: $managed_path"
  done
  [[ "$HERMES_WORKSPACE" != "$HERMES_BACKUP_DIR" ]] ||
    die "workspace and backup directory must be different"

  readonly HERMES_USER_HOME HERMES_HOME HERMES_INSTALL_DIR HERMES_BIN HERMES_NODE_BIN
  readonly HERMES_WORKSPACE HERMES_BACKUP_DIR HERMES_GROUP

  local home_owner_id
  home_owner_id="$(stat -c '%u' "$HERMES_USER_HOME")"
  [[ "$home_owner_id" == "$(id -u "$HERMES_USER")" ]] ||
    die "$HERMES_USER_HOME must be owned by $HERMES_USER"

  install -d -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0750 "$HERMES_WORKSPACE"
  install -d -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0700 "$HERMES_BACKUP_DIR"
}

run_as_hermes_impl() {
  local allow_stdin="$1"
  shift

  local -a runuser_args=(
    --user "$HERMES_USER" -- env -i
    "HOME=$HERMES_USER_HOME"
    "HERMES_HOME=$HERMES_HOME"
    "LANG=${LANG:-C.UTF-8}"
    "LOGNAME=$HERMES_USER"
    "PATH=$HERMES_USER_HOME/.local/bin:$HERMES_NODE_BIN:/usr/local/bin:/usr/bin:/bin"
    SHELL=/bin/bash
    "TERM=${TERM:-xterm-256color}"
    "USER=$HERMES_USER"
    /bin/bash -c 'cd -- "$1"; shift; exec "$@"' bash "$HERMES_USER_HOME" "$@"
  )

  if [[ "$allow_stdin" == true ]]; then
    runuser "${runuser_args[@]}"
  else
    # setsid detaches from any controlling terminal, not just fd 0. Some
    # bundled installers open /dev/tty directly to prompt even when stdin is
    # redirected; without a controlling terminal that open fails immediately
    # instead of hanging forever waiting for input nobody can supply here.
    setsid runuser "${runuser_args[@]}" </dev/null
  fi
}

run_as_hermes() {
  run_as_hermes_impl false "$@"
}

run_as_hermes_interactive() {
  run_as_hermes_impl true "$@"
}

resolve_managed_runtime() {
  local services_output
  local -a gateway_services=()
  services_output="$(python3 "$VPS_CONFIG_APPLIER" services \
    --settings "$VPS_SETTINGS_FILE" gateway)" ||
    die "could not resolve the managed gateway service"
  mapfile -t gateway_services <<<"$services_output"
  ((${#gateway_services[@]} == 1)) ||
    die "vps_services.gateway must contain exactly one service"
  [[ "${gateway_services[0]}" =~ ^[A-Za-z0-9_.@:-]+[.]service$ ]] ||
    die "invalid managed gateway service: ${gateway_services[0]}"
  HERMES_GATEWAY_SERVICE="${gateway_services[0]}"
  HERMES_RAW_BASE_URL="$(python3 "$VPS_CONFIG_APPLIER" value \
    --settings "$VPS_SETTINGS_FILE" vps_deploy.hermes_source.raw_base_url)"
  [[ "$HERMES_RAW_BASE_URL" == "https://raw.githubusercontent.com/NousResearch/hermes-agent" ]] ||
    die "unsupported Hermes source base URL: $HERMES_RAW_BASE_URL"
}

quiesce_existing_gateway_for_update() {
  [[ -x "$HERMES_BIN" ]] || return 0
  command -v systemctl >/dev/null 2>&1 || return 0
  systemctl is-active --quiet "$HERMES_GATEWAY_SERVICE" || return 0

  log "Stopping the managed gateway for a consistent update snapshot"
  systemctl stop "$HERMES_GATEWAY_SERVICE"
  if systemctl is-active --quiet "$HERMES_GATEWAY_SERVICE"; then
    die "$HERMES_GATEWAY_SERVICE remained active after stop"
  fi
  GATEWAY_WAS_QUIESCED=true
}

download_installer() {
  local installer_url="$HERMES_RAW_BASE_URL/$HERMES_COMMIT/scripts/install.sh"
  INSTALLER_FILE="$(mktemp /tmp/hermes-installer.XXXXXX)"
  chmod 0755 "$INSTALLER_FILE"

  log "Downloading the Hermes installer pinned to $HERMES_COMMIT"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "$installer_url" --output "$INSTALLER_FILE"

  local actual_sha256
  actual_sha256="$(sha256sum "$INSTALLER_FILE" | cut -d' ' -f1)"
  [[ "$actual_sha256" == "$INSTALLER_SHA256" ]] ||
    die "installer checksum mismatch (got $actual_sha256)"
  log "Installer checksum verified"
}

backup_existing_installation() {
  if [[ ! -e "$HERMES_BIN" && ! -d "$HERMES_INSTALL_DIR" ]]; then
    if [[ -d "$HERMES_HOME" ]] &&
        find "$HERMES_HOME" -mindepth 1 -print -quit | grep -q .; then
      die "Hermes state exists but its CLI is missing; refusing to install without a verified backup"
    fi
    return 0
  fi
  [[ -x "$HERMES_BIN" ]] ||
    die "existing Hermes CLI is unusable; refusing to update without a verified backup"
  if [[ ! -x "$HERMES_INSTALL_DIR/venv/bin/python" ]]; then
    die "existing Hermes venv interpreter is missing; refusing to update without a verified backup"
  fi
  [[ -f "$UPDATE_STATE_VERIFIER" ]] ||
    die "update-state verifier is missing: $UPDATE_STATE_VERIFIER"

  local timestamp backup_file backup_output
  timestamp="$(date -u +%Y%m%d-%H%M%S)"
  backup_file="$HERMES_BACKUP_DIR/pre-deploy-$timestamp.zip"
  KANBAN_BEFORE_SNAPSHOT="$HERMES_BACKUP_DIR/pre-deploy-$timestamp-kanban-before.json"
  KANBAN_AFTER_SNAPSHOT="$HERMES_BACKUP_DIR/pre-deploy-$timestamp-kanban-after.json"
  UPDATE_GUARD_ACTIVE=true

  if [[ -f "$HERMES_WORKSPACE/AGENTS.md" ]]; then
    install -d -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0700 \
      "$HERMES_HOME/operator-state"
    install -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0600 \
      "$HERMES_WORKSPACE/AGENTS.md" \
      "$HERMES_HOME/operator-state/workspace-AGENTS.md"
  fi

  log "Inventorying Hermes state and checking every Kanban database before the update"
  python3 "$UPDATE_STATE_VERIFIER" snapshot \
    --hermes-home "$HERMES_HOME" \
    --output "$KANBAN_BEFORE_SNAPSHOT"
  chown "$HERMES_USER:$HERMES_GROUP" "$KANBAN_BEFORE_SNAPSHOT"
  chmod 0600 "$KANBAN_BEFORE_SNAPSHOT"

  log "Backing up the existing Hermes data to $backup_file"
  if ! backup_output="$(run_as_hermes "$HERMES_BIN" backup --output "$backup_file" 2>&1)"; then
    printf '%s\n' "$backup_output" >&2
    die "Hermes full backup command failed; update aborted"
  fi
  printf '%s\n' "$backup_output"
  [[ "$backup_output" == *"Backup complete:"* ]] ||
    die "Hermes did not report a complete full backup; update aborted"
  [[ -s "$backup_file" ]] || die "Hermes full backup is missing or empty; update aborted"
  chmod 0600 "$backup_file"

  log "Validating the full backup archive and its Kanban snapshots"
  python3 "$UPDATE_STATE_VERIFIER" verify-backup \
    --backup "$backup_file" \
    --snapshot "$KANBAN_BEFORE_SNAPSHOT"
  log "Mandatory pre-update backup verified"
}

install_hermes() {
  local installer_args=(
    --skip-setup
    --branch "$HERMES_BRANCH"
    --commit "$HERMES_COMMIT"
    --force-commit
    --hermes-home "$HERMES_HOME"
  )

  if [[ "$WITH_BROWSER" == false ]]; then
    installer_args=(--skip-browser "${installer_args[@]}")
  fi

  if [[ "$UPDATE_GUARD_ACTIVE" == true ]]; then
    UPDATE_MUTATION_STARTED=true
  fi
  log "Installing Hermes $HERMES_RELEASE ($HERMES_VERSION, $HERMES_COMMIT) as $HERMES_USER"
  run_as_hermes bash "$INSTALLER_FILE" "${installer_args[@]}"
  [[ -x "$HERMES_BIN" ]] || die "Hermes launcher was not created at $HERMES_BIN"

  local actual_commit actual_version
  actual_commit="$(run_as_hermes git -C "$HERMES_INSTALL_DIR" rev-parse HEAD)"
  [[ "$actual_commit" == "$HERMES_COMMIT" ]] ||
    die "installed Hermes commit mismatch (got $actual_commit)"
  actual_version="$(
    run_as_hermes "$HERMES_INSTALL_DIR/venv/bin/python" -c \
      'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' \
      "$HERMES_INSTALL_DIR/pyproject.toml"
  )"
  [[ "$actual_version" == "$HERMES_VERSION" ]] ||
    die "installed Hermes version mismatch (got $actual_version)"
  log "Hermes source identity verified"
}

verify_updated_kanban_state() {
  [[ "$UPDATE_GUARD_ACTIVE" == true ]] || return 0

  log "Inventorying Hermes state and checking every Kanban database after the update"
  python3 "$UPDATE_STATE_VERIFIER" snapshot \
    --hermes-home "$HERMES_HOME" \
    --output "$KANBAN_AFTER_SNAPSHOT"
  chown "$HERMES_USER:$HERMES_GROUP" "$KANBAN_AFTER_SNAPSHOT"
  chmod 0600 "$KANBAN_AFTER_SNAPSHOT"
  python3 "$UPDATE_STATE_VERIFIER" compare \
    --before "$KANBAN_BEFORE_SNAPSHOT" \
    --after "$KANBAN_AFTER_SNAPSHOT"
  log "Post-update personal files, Kanban integrity, and task counts verified"

  if [[ "$GATEWAY_WAS_QUIESCED" == true && "$ENABLE_GATEWAY" != false ]]; then
    log "Restarting the gateway after successful update verification"
    systemctl start "$HERMES_GATEWAY_SERVICE"
    systemctl is-active --quiet "$HERMES_GATEWAY_SERVICE" ||
      die "$HERMES_GATEWAY_SERVICE did not restart after the verified update"
    GATEWAY_WAS_QUIESCED=false
  fi

  local quick_keep_days full_keep deployment_keep
  quick_keep_days="$(python3 "$VPS_CONFIG_APPLIER" value \
    --settings "$VPS_SETTINGS_FILE" vps_ops.backup.retention_days)"
  full_keep="$(python3 "$VPS_CONFIG_APPLIER" value \
    --settings "$VPS_SETTINGS_FILE" vps_ops.backup.full_keep)"
  deployment_keep="$(python3 "$VPS_CONFIG_APPLIER" value \
    --settings "$VPS_SETTINGS_FILE" vps_ops.backup.deployment_keep)"
  if ! python3 "$SCRIPT_DIR/ops/prune-backups.py" \
      --backup-dir "$HERMES_BACKUP_DIR" \
      --snapshots-dir "$HERMES_HOME/state-snapshots" \
      --quick-retention-days "$quick_keep_days" \
      --full-keep "$full_keep" \
      --deployment-keep "$deployment_keep"; then
    warn "backup retention failed after the verified update"
  fi
}

install_local_browser_automation() {
  [[ "$WITH_BROWSER" == true ]] || return 0
  local browser_installer="$SCRIPT_DIR/ops/install-browser-automation.sh"
  [[ -x "$browser_installer" ]] || {
    warn "local browser automation installer is unavailable: $browser_installer"
    return 0
  }

  log "Installing and verifying local browser automation"
  local agent_browser_version
  local agent_browser_args
  agent_browser_version="$(python3 "$VPS_CONFIG_APPLIER" value \
    --settings "$VPS_SETTINGS_FILE" vps_browser.agent_browser_version)"
  agent_browser_args="$(python3 "$VPS_CONFIG_APPLIER" value \
    --settings "$VPS_SETTINGS_FILE" vps_browser.launch_args)"
  "$browser_installer" \
    --user "$HERMES_USER" \
    --user-home "$HERMES_USER_HOME" \
    --hermes-home "$HERMES_HOME" \
    --version "$agent_browser_version" \
    --launch-args "$agent_browser_args"
}

apply_local_hermes_patches() {
  local patch_applier="$SCRIPT_DIR/runtime/apply-hermes-patches.py"
  [[ -f "$patch_applier" ]] || {
    warn "Hermes patch applier is missing: $patch_applier"
    return 0
  }

  log "Applying local Hermes gateway command patches"
  run_as_hermes env HERMES_INSTALL_DIR="$HERMES_INSTALL_DIR" \
    python3 "$patch_applier"
}

configure_development_clis() {
  [[ "$INSTALL_DEV_CLIS" == true ]] || return 0

  local github_wrapper="$SCRIPT_DIR/runtime/github-cli-wrapper.py"
  local default_branch
  local fetch_prune
  local fetch_prune_tags
  local push_auto_setup_remote
  local pull_ff
  default_branch="$(python3 "$VPS_CONFIG_APPLIER" value --settings "$VPS_SETTINGS_FILE" vps_github.git_defaults.default_branch)"
  fetch_prune="$(python3 "$VPS_CONFIG_APPLIER" value --settings "$VPS_SETTINGS_FILE" vps_github.git_defaults.fetch_prune)"
  fetch_prune_tags="$(python3 "$VPS_CONFIG_APPLIER" value --settings "$VPS_SETTINGS_FILE" vps_github.git_defaults.fetch_prune_tags)"
  push_auto_setup_remote="$(python3 "$VPS_CONFIG_APPLIER" value --settings "$VPS_SETTINGS_FILE" vps_github.git_defaults.push_auto_setup_remote)"
  pull_ff="$(python3 "$VPS_CONFIG_APPLIER" value --settings "$VPS_SETTINGS_FILE" vps_github.git_defaults.pull_ff)"

  log "Configuring safe Git defaults for the Hermes user"
  run_as_hermes git config --global init.defaultBranch "$default_branch"
  run_as_hermes git config --global fetch.prune "$fetch_prune"
  run_as_hermes git config --global fetch.pruneTags "$fetch_prune_tags"
  run_as_hermes git config --global push.autoSetupRemote "$push_auto_setup_remote"
  run_as_hermes git config --global pull.ff "$pull_ff"

  if command -v git-lfs >/dev/null 2>&1; then
    run_as_hermes git lfs install --skip-repo
  fi

  if [[ -x /usr/bin/gh && -f "$github_wrapper" ]]; then
    install -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0750 \
      "$github_wrapper" "$HERMES_USER_HOME/.local/bin/gh"
    run_as_hermes git config --global --replace-all credential.https://github.com.helper ""
    run_as_hermes git config --global --add credential.https://github.com.helper \
      "!$HERMES_USER_HOME/.local/bin/gh auth git-credential"
  elif ! command -v gh >/dev/null 2>&1; then
    warn "GitHub CLI (gh) was not available; regular git clone/pull/push still work"
  else
    warn "managed GitHub CLI wrapper was not found: $github_wrapper"
  fi
}

install_google_workspace_cli() {
  [[ "$INSTALL_GOOGLE_CLI" == true ]] || return 0

  local npm_bin="$HERMES_NODE_BIN/npm"
  local setup_script="$HERMES_INSTALL_DIR/skills/productivity/google-workspace/scripts/setup.py"
  local python_bin="$HERMES_INSTALL_DIR/venv/bin/python"
  local google_cli_version
  local package_json="$HERMES_HOME/node/lib/node_modules/@googleworkspace/cli/package.json"
  local installed_version=""

  [[ -x "$npm_bin" ]] || die "Hermes-managed npm was not found at $npm_bin"
  google_cli_version="$(python3 "$VPS_CONFIG_APPLIER" value \
    --settings "$VPS_SETTINGS_FILE" vps_tools.google_workspace_cli.version)"
  [[ "$google_cli_version" =~ ^[0-9]+[.][0-9]+[.][0-9]+$ ]] ||
    die "invalid pinned Google Workspace CLI version: $google_cli_version"
  if [[ -f "$package_json" ]]; then
    installed_version="$(python3 -c \
      'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' \
      "$package_json")"
  fi

  if [[ "$installed_version" != "$google_cli_version" ]]; then
    log "Installing pinned Google Workspace CLI $google_cli_version"
    run_as_hermes "$npm_bin" install --global --omit=dev \
      --prefix "$HERMES_HOME/node" \
      "@googleworkspace/cli@$google_cli_version"
  else
    log "Pinned Google Workspace CLI $google_cli_version is already installed"
  fi

  if ! run_as_hermes bash -c 'command -v gws >/dev/null 2>&1'; then
    die "gws was installed but is not available in the Hermes user PATH"
  fi

  # The bundled skill prefers gws. Its official Python client is prepared as a
  # fallback when the installed Hermes version supports dependency bootstrap.
  if [[ -x "$python_bin" && -f "$setup_script" ]]; then
    log "Preparing the bundled Google Workspace Python fallback"
    if ! run_as_hermes "$python_bin" "$setup_script" --install-deps; then
      warn "Google Python fallback setup failed; the preferred gws CLI remains installed"
    fi
  else
    warn "bundled Google Workspace fallback setup was not found"
  fi
}

run_setup() {
  if [[ "$ENABLE_GATEWAY" == true ]]; then
    log "If asked about a gateway service, choose 'System service' or skip it; this script will install it"
  fi

  if [[ "$SETUP_PORTAL" == true ]]; then
    log "Starting Nous Portal setup (model plus managed web, image, TTS, and browser tools)"
    run_as_hermes_interactive "$HERMES_BIN" setup --portal
  else
    log "Starting the interactive Hermes setup wizard"
    run_as_hermes_interactive "$HERMES_BIN" setup --quick
  fi
}

run_mcp_picker() {
  log "Opening the Nous-approved MCP catalog"
  log "Install only integrations you actually need"
  log "Built-in web, files, terminal, and Google do not need duplicate MCPs"
  if ! run_as_hermes_interactive "$HERMES_BIN" mcp; then
    warn "MCP picker was cancelled or did not complete; run 'hermes mcp' later"
  fi
}

initialize_skills_hub() {
  log "Initializing the Hermes Skills Hub"
  if ! run_as_hermes "$HERMES_BIN" skills list >/dev/null; then
    warn "Skills Hub initialization did not complete; run 'hermes skills list' as ${HERMES_USER}"
  fi
}

apply_recommended_defaults() {
  local hermes_python="$HERMES_HOME/hermes-agent/venv/bin/python"
  local -a args=(
    apply
    --settings "$VPS_SETTINGS_FILE"
    --hermes-home "$HERMES_HOME"
    --hermes-bin "$HERMES_BIN"
    --workspace "$HERMES_WORKSPACE"
  )

  [[ -f "$VPS_SETTINGS_FILE" ]] || die "VPS settings are missing: $VPS_SETTINGS_FILE"
  [[ -f "$VPS_CONFIG_APPLIER" ]] || die "VPS config applier is missing: $VPS_CONFIG_APPLIER"
  [[ -x "$hermes_python" ]] || die "Hermes Python is missing: $hermes_python"

  # Direct installs can opt into capability-specific routing by exporting only
  # the relevant credential before deployment. Ansible passes these capability
  # names from Vault without exposing credential values.
  [[ -n "${BRAVE_SEARCH_API_KEY:-}" ]] && args+=(--capability brave_search)
  [[ -n "${FIRECRAWL_API_KEY:-}" ]] && args+=(--capability firecrawl_extract)

  log "Applying shared VPS runtime settings"
  run_as_hermes "$hermes_python" "$VPS_CONFIG_APPLIER" "${args[@]}"
}

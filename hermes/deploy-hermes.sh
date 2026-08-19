#!/usr/bin/env bash

set -Eeuo pipefail

umask 027

readonly DEFAULT_HERMES_USER="hermes"
readonly DEFAULT_HERMES_BRANCH="main"
readonly INSTALLER_URL="https://hermes-agent.nousresearch.com/install.sh"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

HERMES_USER="$DEFAULT_HERMES_USER"
HERMES_BRANCH="$DEFAULT_HERMES_BRANCH"
INSTALLER_SHA256=""
WITH_BROWSER=true
RUN_SETUP=true
# Auto-start only after setup has created both Telegram credentials. Use
# --enable-gateway to force it or --no-gateway to skip it explicitly.
ENABLE_GATEWAY=auto
SETUP_PORTAL=false
INSTALL_DEV_CLIS=true
INSTALL_GOOGLE_CLI=true
RUN_MCP_PICKER=true
INSTALL_OPS=true
INSTALL_TAILSCALE=true
RUN_TAILSCALE_LOGIN=true
ALLOW_HOST_ADMIN=true
INSTALLER_FILE=""

log() {
  printf '[hermes-deploy] %s\n' "$*"
}

warn() {
  printf '[hermes-deploy] WARNING: %s\n' "$*" >&2
}

die() {
  printf '[hermes-deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Install Hermes Agent on a Debian/Ubuntu VPS under a dedicated system user.

Usage:
  sudo ./deploy-hermes.sh [options]

Options:
  --user NAME              Service user to create/use (default: hermes)
  --branch NAME            Hermes Git branch to install (default: main)
  --portal                 Configure Nous Portal and its managed web tools
  --without-browser        Do not install Playwright Chromium
  --without-dev-cli        Do not install the extra coding/admin CLI bundle
  --without-google-cli     Do not install Google Workspace CLI (gws)
  --skip-setup             Do not run the interactive setup wizard
  --skip-mcp               Do not open the Nous-approved MCP catalog picker
  --no-gateway             Do not install the systemd messaging gateway
  --without-ops            Do not install audit, metrics, and health alerts
  --without-tailscale      Do not install Tailscale private networking
  --skip-tailscale-login   Install Tailscale but defer interactive login
  --minimal                Install only the base Hermes CLI
  --with-browser           Explicitly enable Chromium (the default)
  --with-dev-cli           Explicitly enable the coding/admin CLI bundle
  --with-google-cli        Explicitly enable Google Workspace CLI (the default)
  --setup                  Explicitly enable the setup wizard (the default)
  --setup-mcp              Explicitly open the MCP picker (the default)
  --enable-gateway         Explicitly enable the gateway even without Telegram credentials
  --with-ops               Explicitly enable audit, metrics, and alerts (the default)
  --with-tailscale         Explicitly install Tailscale (the default)
  --tailscale-login        Explicitly run Tailscale login (the default)
  --allow-host-admin       Give Hermes passwordless sudo (the default)
  --without-host-admin     Keep Hermes restricted to its service account
  --installer-sha256 HASH  Require this SHA-256 for the downloaded installer
  -h, --help               Show this help

Examples:
  sudo ./deploy-hermes.sh
  sudo ./deploy-hermes.sh --portal
  sudo ./deploy-hermes.sh --minimal
EOF
}

require_option_value() {
  local option="$1"
  local value="${2:-}"

  if [[ -z "$value" || "$value" == -* ]]; then
    die "$option requires a value"
  fi
}

while (($# > 0)); do
  case "$1" in
    --user)
      require_option_value "$1" "${2:-}"
      HERMES_USER="$2"
      shift 2
      ;;
    --branch)
      require_option_value "$1" "${2:-}"
      HERMES_BRANCH="$2"
      shift 2
      ;;
    --portal)
      SETUP_PORTAL=true
      RUN_SETUP=true
      shift
      ;;
    --without-browser)
      WITH_BROWSER=false
      shift
      ;;
    --without-dev-cli)
      INSTALL_DEV_CLIS=false
      shift
      ;;
    --without-google-cli)
      INSTALL_GOOGLE_CLI=false
      shift
      ;;
    --skip-setup)
      RUN_SETUP=false
      SETUP_PORTAL=false
      RUN_MCP_PICKER=false
      shift
      ;;
    --skip-mcp)
      RUN_MCP_PICKER=false
      shift
      ;;
    --no-gateway)
      ENABLE_GATEWAY=false
      shift
      ;;
    --without-ops)
      INSTALL_OPS=false
      shift
      ;;
    --without-tailscale)
      INSTALL_TAILSCALE=false
      RUN_TAILSCALE_LOGIN=false
      shift
      ;;
    --skip-tailscale-login)
      RUN_TAILSCALE_LOGIN=false
      shift
      ;;
    --minimal)
      WITH_BROWSER=false
      RUN_SETUP=false
      ENABLE_GATEWAY=false
      SETUP_PORTAL=false
      INSTALL_DEV_CLIS=false
      INSTALL_GOOGLE_CLI=false
      RUN_MCP_PICKER=false
      INSTALL_OPS=false
      INSTALL_TAILSCALE=false
      RUN_TAILSCALE_LOGIN=false
      shift
      ;;
    --with-browser)
      WITH_BROWSER=true
      shift
      ;;
    --with-dev-cli)
      INSTALL_DEV_CLIS=true
      shift
      ;;
    --with-google-cli)
      INSTALL_GOOGLE_CLI=true
      shift
      ;;
    --setup)
      RUN_SETUP=true
      shift
      ;;
    --setup-mcp)
      RUN_MCP_PICKER=true
      shift
      ;;
    --enable-gateway)
      ENABLE_GATEWAY=true
      shift
      ;;
    --with-ops)
      INSTALL_OPS=true
      shift
      ;;
    --with-tailscale)
      INSTALL_TAILSCALE=true
      shift
      ;;
    --tailscale-login)
      INSTALL_TAILSCALE=true
      RUN_TAILSCALE_LOGIN=true
      shift
      ;;
    --allow-host-admin)
      ALLOW_HOST_ADMIN=true
      shift
      ;;
    --without-host-admin)
      ALLOW_HOST_ADMIN=false
      shift
      ;;
    --installer-sha256)
      require_option_value "$1" "${2:-}"
      INSTALLER_SHA256="${2,,}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

cleanup() {
  if [[ -n "$INSTALLER_FILE" && -f "$INSTALLER_FILE" ]]; then
    rm -f -- "$INSTALLER_FILE"
  fi
}

trap cleanup EXIT
trap 'die "command failed at line $LINENO"' ERR

validate_inputs() {
  [[ "$(uname -s)" == "Linux" ]] || die "only Linux VPS hosts are supported"
  ((EUID == 0)) || die "run this script as root (for example, with sudo)"

  [[ "$HERMES_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] ||
    die "invalid service user: $HERMES_USER"
  [[ "$HERMES_USER" != "root" ]] || die "refusing to run Hermes as root"
  [[ "$HERMES_BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] ||
    die "invalid branch name: $HERMES_BRANCH"

  if [[ -n "$INSTALLER_SHA256" && ! "$INSTALLER_SHA256" =~ ^[a-f0-9]{64}$ ]]; then
    die "--installer-sha256 must be 64 hexadecimal characters"
  fi

  if [[ ("$RUN_SETUP" == true || "$RUN_MCP_PICKER" == true || "$RUN_TAILSCALE_LOGIN" == true) && ! -t 0 ]]; then
    die "the setup wizard, MCP picker, and Tailscale login need an interactive terminal"
  fi
}

install_host_dependencies() {
  [[ -r /etc/os-release ]] || die "cannot identify the Linux distribution"

  # shellcheck disable=SC1091
  source /etc/os-release
  local distro_words=" ${ID:-} ${ID_LIKE:-} "
  [[ "$distro_words" == *" debian "* || "$distro_words" == *" ubuntu "* ]] ||
    die "this script supports Debian/Ubuntu hosts; detected ${ID:-unknown}"

  log "Installing host dependencies"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    ffmpeg \
    git \
    passwd \
    ripgrep \
    util-linux \
    xz-utils

  if [[ "$INSTALL_DEV_CLIS" == true ]]; then
    install_development_clis
  fi

  if [[ "$WITH_BROWSER" == true ]]; then
    install_browser_os_dependencies
  fi
}

install_tailscale() {
  [[ "$INSTALL_TAILSCALE" == true ]] || return 0

  # shellcheck disable=SC1091
  source /etc/os-release
  local distro="${ID:-}"
  local codename="${VERSION_CODENAME:-}"
  [[ "$distro" == "debian" || "$distro" == "ubuntu" ]] ||
    die "the official Tailscale repository is only configured here for Debian/Ubuntu"
  [[ "$codename" =~ ^[a-z0-9-]+$ ]] || die "cannot determine a safe distribution codename"

  local repository_base="https://pkgs.tailscale.com/stable/${distro}/${codename}"
  local temporary_dir
  temporary_dir="$(mktemp -d /tmp/hermes-tailscale.XXXXXX)"

  log "Adding the official Tailscale package repository"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "${repository_base}.noarmor.gpg" --output "${temporary_dir}/tailscale-archive-keyring.gpg"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "${repository_base}.tailscale-keyring.list" --output "${temporary_dir}/tailscale.list"
  install -o root -g root -m 0644 \
    "${temporary_dir}/tailscale-archive-keyring.gpg" /usr/share/keyrings/tailscale-archive-keyring.gpg
  install -o root -g root -m 0644 \
    "${temporary_dir}/tailscale.list" /etc/apt/sources.list.d/tailscale.list
  rm -rf -- "${temporary_dir}"

  apt-get update
  apt-get install -y --no-install-recommends tailscale
  systemctl enable --now tailscaled.service
}

run_tailscale_login() {
  [[ "$INSTALL_TAILSCALE" == true && "$RUN_TAILSCALE_LOGIN" == true ]] || return 0

  log "Connecting the VPS to Tailscale and enabling Tailscale SSH"
  if tailscale ip -4 >/dev/null 2>&1; then
    tailscale set --ssh
  else
    tailscale up --ssh
  fi
  tailscale ip -4 >/dev/null 2>&1 || die "Tailscale did not receive a private IP"
}

install_development_clis() {
  local package
  local -a requested_packages=(
    build-essential
    dnsutils
    file
    gh
    git-lfs
    jq
    lsof
    netcat-openbsd
    openssh-client
    pkg-config
    rsync
    shellcheck
    sqlite3
    tree
    unzip
    wget
    zip
  )
  local -a available_packages=()

  log "Installing coding, GitHub, data, archive, and VPS diagnostic CLIs"
  for package in "${requested_packages[@]}"; do
    if apt-cache show "$package" >/dev/null 2>&1; then
      available_packages+=("$package")
    else
      warn "optional CLI package is unavailable in this distribution: $package"
    fi
  done

  if ((${#available_packages[@]} > 0)); then
    apt-get install -y --no-install-recommends "${available_packages[@]}"
  fi
}

install_browser_os_dependencies() {
  local package
  local candidate
  local -a browser_packages=(
    fonts-liberation
    fonts-noto-color-emoji
    libasound2
    libatk-bridge2.0-0
    libatk1.0-0
    libatspi2.0-0
    libcairo2
    libcups2
    libdbus-1-3
    libdrm2
    libegl1
    libfontconfig1
    libfreetype6
    libgbm1
    libglib2.0-0
    libgtk-3-0
    libnspr4
    libnss3
    libpango-1.0-0
    libwayland-client0
    libx11-6
    libx11-xcb1
    libxcb1
    libxcomposite1
    libxdamage1
    libxext6
    libxfixes3
    libxkbcommon0
    libxrandr2
    libxshmfence1
    xvfb
  )
  local -a resolved_packages=()

  log "Resolving Chromium system libraries"
  for package in "${browser_packages[@]}"; do
    candidate="$package"
    # Ubuntu 24.04 exposes libasound2 as a virtual package. `apt-cache show`
    # still exits successfully for it, but apt-get cannot install the virtual
    # name; prefer the concrete t64 provider when available.
    if [[ "$package" == "libasound2" ]] && apt-cache show libasound2t64 >/dev/null 2>&1; then
      candidate="libasound2t64"
    elif ! apt-cache show "$candidate" >/dev/null 2>&1; then
      candidate="${package}t64"
      apt-cache show "$candidate" >/dev/null 2>&1 ||
        die "required Chromium package is unavailable: $package"
    fi
    resolved_packages+=("$candidate")
  done

  apt-get install -y --no-install-recommends "${resolved_packages[@]}"
}

ensure_service_user() {
  if id "$HERMES_USER" >/dev/null 2>&1; then
    log "Using existing user: $HERMES_USER"
  else
    log "Creating unprivileged user: $HERMES_USER"
    useradd --create-home --shell /bin/bash "$HERMES_USER"
  fi

  local user_id
  user_id="$(id -u "$HERMES_USER")"
  [[ "$user_id" != "0" ]] || die "the Hermes service user must not be root"

  if id -nG "$HERMES_USER" | tr ' ' '\n' | grep -Eq '^(sudo|wheel)$'; then
    warn "$HERMES_USER belongs to an administrative group; a dedicated non-sudo user is recommended"
  fi
}

enable_host_administration() {
  [[ "$ALLOW_HOST_ADMIN" == true ]] || return 0
  command -v visudo >/dev/null 2>&1 || die "visudo is required for --allow-host-admin"
  local sudoers_file="/etc/sudoers.d/hermes-host-admin"
  local temporary_file
  temporary_file="$(mktemp)"
  printf '# Explicit owner opt-in: Hermes can administer this host via sudo.\n%s ALL=(ALL:ALL) NOPASSWD: ALL\n' \
    "$HERMES_USER" >"$temporary_file"
  visudo -cf "$temporary_file" >/dev/null || die "generated sudoers policy is invalid"
  install -o root -g root -m 0440 "$temporary_file" "$sudoers_file"
  rm -f -- "$temporary_file"
  log "Hermes host administration is enabled; this is equivalent to root access"
}

resolve_user_paths() {
  local passwd_entry
  passwd_entry="$(getent passwd "$HERMES_USER")" ||
    die "cannot read the passwd entry for $HERMES_USER"

  HERMES_USER_HOME="$(cut -d: -f6 <<<"$passwd_entry")"
  [[ "$HERMES_USER_HOME" == /* && "$HERMES_USER_HOME" != "/" ]] ||
    die "unsafe home directory for $HERMES_USER: $HERMES_USER_HOME"
  [[ -d "$HERMES_USER_HOME" ]] || die "home directory does not exist: $HERMES_USER_HOME"

  HERMES_HOME="$HERMES_USER_HOME/.hermes"
  HERMES_INSTALL_DIR="$HERMES_HOME/hermes-agent"
  HERMES_BIN="$HERMES_USER_HOME/.local/bin/hermes"
  HERMES_NODE_BIN="$HERMES_HOME/node/bin"
  HERMES_WORKSPACE="$HERMES_USER_HOME/workspace"
  HERMES_BACKUP_DIR="$HERMES_USER_HOME/hermes-backups"
  HERMES_GROUP="$(id -gn "$HERMES_USER")"

  readonly HERMES_USER_HOME HERMES_HOME HERMES_INSTALL_DIR HERMES_BIN HERMES_NODE_BIN
  readonly HERMES_WORKSPACE HERMES_BACKUP_DIR HERMES_GROUP

  local home_owner_id
  home_owner_id="$(stat -c '%u' "$HERMES_USER_HOME")"
  [[ "$home_owner_id" == "$(id -u "$HERMES_USER")" ]] ||
    die "$HERMES_USER_HOME must be owned by $HERMES_USER"

  install -d -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0750 "$HERMES_WORKSPACE"
  install -d -o "$HERMES_USER" -g "$HERMES_GROUP" -m 0700 "$HERMES_BACKUP_DIR"
}

run_as_hermes() {
  runuser --user "$HERMES_USER" -- env -i \
    HOME="$HERMES_USER_HOME" \
    HERMES_HOME="$HERMES_HOME" \
    LANG="${LANG:-C.UTF-8}" \
    LOGNAME="$HERMES_USER" \
    PATH="$HERMES_USER_HOME/.local/bin:$HERMES_NODE_BIN:/usr/local/bin:/usr/bin:/bin" \
    SHELL=/bin/bash \
    TERM="${TERM:-xterm-256color}" \
    USER="$HERMES_USER" \
    /bin/bash -c 'cd -- "$1"; shift; exec "$@"' bash "$HERMES_USER_HOME" "$@" </dev/null
}

download_installer() {
  INSTALLER_FILE="$(mktemp /tmp/hermes-installer.XXXXXX)"
  chmod 0755 "$INSTALLER_FILE"

  log "Downloading the official Hermes installer"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "$INSTALLER_URL" --output "$INSTALLER_FILE"

  if [[ -n "$INSTALLER_SHA256" ]]; then
    local actual_sha256
    actual_sha256="$(sha256sum "$INSTALLER_FILE" | cut -d' ' -f1)"
    [[ "$actual_sha256" == "$INSTALLER_SHA256" ]] ||
      die "installer checksum mismatch (got $actual_sha256)"
    log "Installer checksum verified"
  fi
}

backup_existing_installation() {
  [[ -x "$HERMES_BIN" ]] || return 0

  local backup_file
  backup_file="$HERMES_BACKUP_DIR/pre-deploy-$(date -u +%Y%m%d-%H%M%S).zip"

  log "Backing up the existing Hermes data to $backup_file"
  run_as_hermes "$HERMES_BIN" backup --output "$backup_file"
  chmod 0600 "$backup_file"
}

install_hermes() {
  local installer_args=(
    --skip-setup
    --branch "$HERMES_BRANCH"
    --hermes-home "$HERMES_HOME"
  )

  if [[ "$WITH_BROWSER" == false ]]; then
    installer_args=(--skip-browser "${installer_args[@]}")
  fi

  log "Installing Hermes branch '$HERMES_BRANCH' as $HERMES_USER"
  run_as_hermes bash "$INSTALLER_FILE" "${installer_args[@]}"
  [[ -x "$HERMES_BIN" ]] || die "Hermes launcher was not created at $HERMES_BIN"
}

install_local_browser_automation() {
  [[ "$WITH_BROWSER" == true ]] || return 0
  local browser_installer="$SCRIPT_DIR/ops/install-browser-automation.sh"
  [[ -x "$browser_installer" ]] || {
    warn "local browser automation installer is unavailable: $browser_installer"
    return 0
  }

  log "Installing and verifying local browser automation"
  "$browser_installer" \
    --user "$HERMES_USER" \
    --user-home "$HERMES_USER_HOME" \
    --hermes-home "$HERMES_HOME"
}

configure_development_clis() {
  [[ "$INSTALL_DEV_CLIS" == true ]] || return 0

  log "Configuring safe Git defaults for the Hermes user"
  run_as_hermes git config --global init.defaultBranch main
  run_as_hermes git config --global fetch.prune true
  run_as_hermes git config --global push.autoSetupRemote true

  if command -v git-lfs >/dev/null 2>&1; then
    run_as_hermes git lfs install --skip-repo
  fi

  if ! command -v gh >/dev/null 2>&1; then
    warn "GitHub CLI (gh) was not available; regular git clone/pull/push still work"
  fi
}

install_google_workspace_cli() {
  [[ "$INSTALL_GOOGLE_CLI" == true ]] || return 0

  local npm_bin="$HERMES_NODE_BIN/npm"
  local setup_script="$HERMES_INSTALL_DIR/skills/productivity/google-workspace/scripts/setup.py"
  local python_bin="$HERMES_INSTALL_DIR/venv/bin/python"

  [[ -x "$npm_bin" ]] || die "Hermes-managed npm was not found at $npm_bin"

  log "Installing Google Workspace CLI (gws) for Gmail, Calendar, Drive, Docs, and Sheets"
  run_as_hermes "$npm_bin" install --global --omit=dev \
    --prefix "$HERMES_HOME/node" \
    @googleworkspace/cli

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
    run_as_hermes "$HERMES_BIN" setup --portal
  else
    log "Starting the interactive Hermes setup wizard"
    run_as_hermes "$HERMES_BIN" setup --quick
  fi
}

run_mcp_picker() {
  log "Opening the Nous-approved MCP catalog"
  log "Install only integrations you actually need"
  log "Built-in web, files, terminal, and Google do not need duplicate MCPs"
  if ! run_as_hermes "$HERMES_BIN" mcp; then
    warn "MCP picker was cancelled or did not complete; run 'hermes mcp' later"
  fi
}

apply_recommended_defaults() {
  log "Applying recommended VPS defaults"
  run_as_hermes "$HERMES_BIN" config set terminal.cwd "$HERMES_WORKSPACE"
  run_as_hermes "$HERMES_BIN" config set checkpoints.enabled true
  run_as_hermes "$HERMES_BIN" config set updates.pre_update_backup full
  run_as_hermes "$HERMES_BIN" config set updates.non_interactive_local_changes stash
  run_as_hermes "$HERMES_BIN" config set gateway.systemd_watchdog_seconds 120
  run_as_hermes "$HERMES_BIN" config set agent.hard_stop_enabled true
  run_as_hermes "$HERMES_BIN" config set agent.loop_caps.max_web_searches 20
  run_as_hermes "$HERMES_BIN" config set agent.loop_caps.max_subagents 10
  run_as_hermes "$HERMES_BIN" config set agent.verify_on_stop auto

  # Newer Hermes versions call this backend "brave-free", but it still needs
  # BRAVE_SEARCH_API_KEY. A shared configured backend (such as Nous Firecrawl)
  # has higher priority than auto-detection, so select Brave explicitly when
  # its managed env entry exists. Keep the shared backend for page extraction.
  if [[ -f "$HERMES_HOME/.env" ]] && grep -q '^BRAVE_SEARCH_API_KEY=' "$HERMES_HOME/.env"; then
    run_as_hermes "$HERMES_BIN" config set web.search_backend brave-free
  else
    run_as_hermes "$HERMES_BIN" config unset web.search_backend || true
  fi

  # Firecrawl supports page extraction and browser-backed interaction. Select
  # it only when its key is managed on this host; otherwise preserve an
  # existing extract backend such as a Nous Portal configuration.
  if [[ -f "$HERMES_HOME/.env" ]] && grep -q '^FIRECRAWL_API_KEY=' "$HERMES_HOME/.env"; then
    run_as_hermes "$HERMES_BIN" config set web.extract_backend firecrawl
  fi

  # OpenRouter exposes model-dependent output limits. A conservative default
  # prevents an advertised 64K completion cap from exhausting a low-credit
  # account. Preserve an explicit model.max_tokens from the Ansible overlay.
  local configured_model_max_tokens
  configured_model_max_tokens="$(run_as_hermes "$HERMES_BIN" config get model.max_tokens 2>/dev/null || true)"
  if [[ -z "${configured_model_max_tokens//[[:space:]]/}" ]]; then
    run_as_hermes "$HERMES_BIN" config set model.max_tokens 4096
  fi
}

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
  )
  if [[ "$ENABLE_GATEWAY" == false ]]; then
    args+=(--defer-timers)
  fi

  log "Installing audit logs, usage metrics, and zero-token health alerts"
  "$SCRIPT_DIR/ops/install-ops.sh" "${args[@]}"
}

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

main() {
  validate_inputs
  install_host_dependencies
  install_tailscale
  ensure_service_user
  resolve_user_paths
  enable_host_administration
  download_installer
  backup_existing_installation
  install_hermes
  install_local_browser_automation
  configure_development_clis
  install_google_workspace_cli

  if [[ "$RUN_SETUP" == true ]]; then
    run_setup
  fi

  if [[ "$RUN_MCP_PICKER" == true ]]; then
    run_mcp_picker
  fi

  apply_recommended_defaults
  resolve_gateway_choice

  if [[ "$ENABLE_GATEWAY" == true ]]; then
    enable_gateway
  fi

  install_operations_layer
  run_tailscale_login

  run_diagnostics

  print_summary
}

main

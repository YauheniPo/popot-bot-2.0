#!/usr/bin/env bash

set -Eeuo pipefail

umask 027

readonly DEFAULT_HERMES_USER="hermes"
readonly HERMES_RAW_BASE_URL="https://raw.githubusercontent.com/NousResearch/hermes-agent"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly VPS_SETTINGS_FILE="$SCRIPT_DIR/config/vps-defaults.yml"
readonly VPS_CONFIG_APPLIER="$SCRIPT_DIR/runtime/apply-config.py"
readonly UPDATE_STATE_VERIFIER="$SCRIPT_DIR/runtime/verify-update-state.py"

HERMES_USER="$DEFAULT_HERMES_USER"
# The deploy source pin (branch/version/release/commit/installer SHA-256) is
# read from VPS_SETTINGS_FILE by resolve_source_pin; CLI flags override it.
HERMES_BRANCH=""
HERMES_VERSION=""
HERMES_RELEASE=""
HERMES_COMMIT=""
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
GATEWAY_WAS_QUIESCED=false
UPDATE_MUTATION_STARTED=false
UPDATE_GUARD_ACTIVE=false
KANBAN_BEFORE_SNAPSHOT=""
KANBAN_AFTER_SNAPSHOT=""

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
  --expected-version VER   Expected Hermes package version after install
  --release TAG            Human-readable Hermes release tag
  --commit SHA             Exact 40-character Hermes commit to install
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
  --installer-sha256 HASH  SHA-256 of install.sh from the selected commit
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
    --expected-version)
      require_option_value "$1" "${2:-}"
      HERMES_VERSION="$2"
      shift 2
      ;;
    --release)
      require_option_value "$1" "${2:-}"
      HERMES_RELEASE="$2"
      shift 2
      ;;
    --commit)
      require_option_value "$1" "${2:-}"
      HERMES_COMMIT="${2,,}"
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
  if [[ "$GATEWAY_WAS_QUIESCED" == true && "$UPDATE_MUTATION_STARTED" == false ]] && \
      command -v systemctl >/dev/null 2>&1; then
    warn "Update stopped before code changes; restarting the previously active gateway"
    systemctl start hermes-gateway.service ||
      warn "Could not restart hermes-gateway.service after the aborted update"
  fi
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
  [[ "$HERMES_VERSION" =~ ^[0-9]+[.][0-9]+[.][0-9]+$ ]] ||
    die "invalid Hermes version: $HERMES_VERSION"
  [[ "$HERMES_RELEASE" =~ ^v[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}$ ]] ||
    die "invalid Hermes release: $HERMES_RELEASE"
  [[ "$HERMES_COMMIT" =~ ^[a-f0-9]{40}$ ]] ||
    die "--commit must be a full 40-character hexadecimal SHA"

  if [[ ! "$INSTALLER_SHA256" =~ ^[a-f0-9]{64}$ ]]; then
    die "--installer-sha256 must be 64 hexadecimal characters"
  fi

  if [[ ("$RUN_SETUP" == true || "$RUN_MCP_PICKER" == true || "$RUN_TAILSCALE_LOGIN" == true) && ! -t 0 ]]; then
    die "the setup wizard, MCP picker, and Tailscale login need an interactive terminal"
  fi
}

# Domain modules share the validated deployment context defined above.
# shellcheck source=deploy/host.sh
source "$SCRIPT_DIR/deploy/host.sh"

resolve_source_pin() {
  # The deploy source pin lives in vps-defaults.yml (single source of truth,
  # shared with Ansible and apply-config.py). System python3 with PyYAML is
  # available on every supported host before provisioning installs anything.
  [[ -r "$VPS_SETTINGS_FILE" ]] ||
    die "VPS settings file is missing or unreadable: $VPS_SETTINGS_FILE"
  command -v python3 >/dev/null 2>&1 ||
    die "python3 is required to read the deploy source pin"

  # The script runs as root and the parsed values drive a checksum-verified
  # download, so refuse a settings file that a non-owner could have tampered
  # with (symlink swap or world/group-writable edit).
  local settings_owner settings_perms
  settings_owner="$(stat -c '%U' "$VPS_SETTINGS_FILE")"
  settings_perms="$(stat -c '%a' "$VPS_SETTINGS_FILE")"
  [[ "$settings_owner" == "root" ]] ||
    die "$VPS_SETTINGS_FILE must be owned by root (found: $settings_owner)"
  [[ "$settings_perms" =~ ^[0-7]00$ ]] ||
    die "$VPS_SETTINGS_FILE must not be group- or world-writable (mode: $settings_perms)"

  local parsed
  parsed="$(python3 - "$VPS_SETTINGS_FILE" <<'PY'
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required to read the deploy source pin")

with open(sys.argv[1]) as handle:
    data = yaml.safe_load(handle)
try:
    source = data["vps_deploy"]["source"]
except (TypeError, KeyError):
    sys.exit("vps-defaults.yml is missing the vps_deploy.source mapping")
for key in ("branch", "version", "release", "commit", "installer_sha256"):
    if key not in source:
        sys.exit(f"vps_deploy.source is missing the {key!r} key")
    value = source[key]
    if not isinstance(value, str) or not value:
        sys.exit(f"deploy source pin {key!r} must be a non-empty string")
print(" ".join(source[key] for key in ("branch", "version", "release", "commit", "installer_sha256")))
PY
)" || die "could not read the deploy source pin from $VPS_SETTINGS_FILE"

  read -r HERMES_BRANCH HERMES_VERSION HERMES_RELEASE HERMES_COMMIT INSTALLER_SHA256 <<<"$parsed"
}

# shellcheck source=deploy/runtime.sh
source "$SCRIPT_DIR/deploy/runtime.sh"
# shellcheck source=deploy/services.sh
source "$SCRIPT_DIR/deploy/services.sh"
# shellcheck source=deploy/reporting.sh
source "$SCRIPT_DIR/deploy/reporting.sh"

main() {
  resolve_source_pin
  validate_inputs
  install_host_dependencies
  install_tailscale
  ensure_service_user
  resolve_user_paths
  enable_host_administration
  quiesce_existing_gateway_for_update
  download_installer
  backup_existing_installation
  install_hermes
  verify_updated_kanban_state
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
  initialize_skills_hub
  resolve_gateway_choice

  if [[ "$ENABLE_GATEWAY" == true ]]; then
    enable_gateway
  fi

  install_operations_layer
  restart_managed_runtime
  run_tailscale_login

  run_diagnostics

  print_summary
}

main

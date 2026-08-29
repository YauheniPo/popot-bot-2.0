# shellcheck shell=bash

install_host_dependencies() {
  [[ -r /etc/os-release ]] || die "cannot identify the Linux distribution"

  # shellcheck disable=SC1091
  source /etc/os-release
  local distro_words=" ${ID:-} ${ID_LIKE:-} "
  [[ "$distro_words" == *" debian "* || "$distro_words" == *" ubuntu "* ]] ||
    die "this script supports Debian/Ubuntu hosts; detected ${ID:-unknown}"

  local -a packages=(
    ca-certificates
    curl
    ffmpeg
    git
    passwd
    python3
    python3-yaml
    ripgrep
    util-linux
    xz-utils
  )
  if [[ "$ALLOW_HOST_ADMIN" == true ]]; then
    # visudo is provided by sudo and is required before creating the explicitly
    # opted-in sudoers policy below. Fresh minimal images may not include it.
    packages+=(sudo)
  fi

  log "Installing host dependencies"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends "${packages[@]}"

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

  local channel
  channel="$(python3 "$VPS_CONFIG_APPLIER" value \
    --settings "$VPS_SETTINGS_FILE" vps_network.tailscale.package_channel)"
  [[ "$channel" == "stable" || "$channel" == "unstable" ]] ||
    die "invalid Tailscale package channel: $channel"
  local repository_base="https://pkgs.tailscale.com/${channel}/${distro}/${codename}"
  local temporary_dir
  temporary_dir="$(mktemp -d /tmp/hermes-tailscale.XXXXXX)"

  log "Adding the official Tailscale package repository"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "${repository_base}.noarmor.gpg" --output "${temporary_dir}/tailscale-archive-keyring.gpg"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "${repository_base}.tailscale-keyring.list" --output "${temporary_dir}/tailscale.list"
  # The .list file is not signed; refuse a substituted or truncated repository
  # definition before it can change what apt-get installs as root.
  grep -Eq "^deb[[:space:]]+.*https://pkgs[.]tailscale[.]com/${channel}/" \
    "${temporary_dir}/tailscale.list" ||
    die "the Tailscale repository list has an unexpected format"
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
    ansible-core
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
    python3-pytest
    python3-yaml
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
    local -a useradd_args=(--create-home --shell /bin/bash)
    if [[ -n "$REQUESTED_USER_HOME" ]]; then
      useradd_args+=(--home-dir "$REQUESTED_USER_HOME")
    fi
    useradd "${useradd_args[@]}" "$HERMES_USER"
  fi

  local user_id
  user_id="$(id -u "$HERMES_USER")"
  [[ "$user_id" != "0" ]] || die "the Hermes service user must not be root"

  if id -nG "$HERMES_USER" | tr ' ' '\n' | grep -Eq '^(sudo|wheel)$'; then
    warn "$HERMES_USER belongs to an administrative group; a dedicated non-sudo user is recommended"
  fi
}

enable_host_administration() {
  local sudoers_file="/etc/sudoers.d/hermes-host-admin"

  if [[ "$ALLOW_HOST_ADMIN" != true ]]; then
    if [[ -e "$sudoers_file" ]]; then
      rm -f -- "$sudoers_file"
      log "Hermes host administration authorization was removed"
    fi
    return 0
  fi

  command -v visudo >/dev/null 2>&1 || die "visudo is required for --allow-host-admin"
  local temporary_file
  temporary_file="$(mktemp)"
  printf '# Explicit owner opt-in: Hermes can administer this host via sudo.\n%s ALL=(ALL:ALL) NOPASSWD: ALL\n' \
    "$HERMES_USER" >"$temporary_file"
  visudo -cf "$temporary_file" >/dev/null || die "generated sudoers policy is invalid"
  install -o root -g root -m 0440 "$temporary_file" "$sudoers_file"
  rm -f -- "$temporary_file"
  log "Hermes host administration is enabled; this is equivalent to root access"
}

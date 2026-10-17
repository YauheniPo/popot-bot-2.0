# shellcheck shell=bash

APT_RETRY_ATTEMPTS=6
APT_RETRY_DELAY_SECONDS=20

# Ubuntu's unattended-upgrades timer can hold /var/lib/dpkg/lock-frontend for
# a minute or two in the background. Retry instead of failing the whole
# deploy on that transient lock contention.
apt_get_retry() {
    local attempt
    for ((attempt = 1; attempt <= APT_RETRY_ATTEMPTS; attempt++)); do
        if DEBIAN_FRONTEND=noninteractive apt-get "$@"; then
            return 0
        fi
        log "apt-get busy (dpkg lock likely held by unattended-upgrades), retrying in ${APT_RETRY_DELAY_SECONDS}s (attempt ${attempt}/${APT_RETRY_ATTEMPTS})"
        sleep "${APT_RETRY_DELAY_SECONDS}"
    done
    die "apt-get failed after ${APT_RETRY_ATTEMPTS} attempts: apt-get $*"
}

install_observability_dependencies() {
    local grafana_channel repository repository_changed=false package
    local -a missing_packages=()
    APT_RETRY_ATTEMPTS="$(managed_value vps_packages.apt_retry_attempts)"
    APT_RETRY_DELAY_SECONDS="$(managed_value vps_packages.apt_retry_delay_seconds)"
    grafana_channel="$(managed_value vps_packages.grafana_channel)"
    [[ "${APT_RETRY_ATTEMPTS}" =~ ^[1-9][0-9]?$ ]] || die "invalid apt retry attempts"
    [[ "${APT_RETRY_DELAY_SECONDS}" =~ ^[1-9][0-9]{0,2}$ ]] || die "invalid apt retry delay"
    [[ "${grafana_channel}" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid Grafana channel"
    command -v apt-get >/dev/null 2>&1 || die "Grafana observability requires apt-get on Debian/Ubuntu"
    command -v curl >/dev/null 2>&1 || die "curl is required to install the official Grafana package repository"

    log "installing Prometheus, node exporter, and Grafana"
    install -d -o root -g root -m 0755 /etc/apt/keyrings
    # `openssl` is needed only for the direct-deploy fallback password. Install
    # it before creating that file; Ansible normally creates the file from Vault.
    if ! package_installed openssl; then
        apt_get_retry update
        apt_get_retry install -y --no-install-recommends openssl
        mark_ops_change
    fi
    # Prepare Grafana's password and unit override before package installation:
    # the package manager may otherwise start Grafana with its default admin
    # account before our first-run credentials are visible.
    install_grafana_password_file
    install -d -o root -g root -m 0755 /etc/systemd/system/grafana-server.service.d
    render \
        "${SCRIPT_DIR}/systemd/grafana-hermes-loopback.conf" /etc/systemd/system/grafana-server.service.d/hermes-loopback.conf 0644 root grafana-server.service
    systemctl daemon-reload
    # Prevent package post-install scripts from exposing the distribution
    # Prometheus listeners before Hermes installs its loopback-only units.
    # These distribution units remain intentionally masked afterwards.
    systemctl stop prometheus.service prometheus-node-exporter.service 2>/dev/null || true
    systemctl mask prometheus.service prometheus-node-exporter.service
    for package in grafana prometheus prometheus-node-exporter; do
        if ! package_installed "$package"; then
            missing_packages+=("$package")
            case "$package" in
                grafana) mark_ops_change grafana-server.service;;
                prometheus) mark_ops_change hermes-prometheus.service;;
                prometheus-node-exporter) mark_ops_change hermes-node-exporter.service;;
            esac
        fi
    done
    # Refresh the key before installing missing packages, including recovery
    # after key rotation. Warm deployments need no download.
    if [[ ! -s /etc/apt/keyrings/grafana.asc ]] || ((${#missing_packages[@]} > 0)); then
        local key_temp
        key_temp="$(mktemp)"
        curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
            --connect-timeout 10 --max-time 60 \
            https://apt.grafana.com/gpg-full.key --output "$key_temp"
        [[ -s "$key_temp" ]] || die "Grafana repository key is empty"
        install_managed_file "$key_temp" /etc/apt/keyrings/grafana.asc 0644 root root
        rm -f "$key_temp"
        repository_changed=true
    fi
    chown root:root /etc/apt/keyrings/grafana.asc
    chmod 0644 /etc/apt/keyrings/grafana.asc
    repository="deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.grafana.com ${grafana_channel} main"
    if [[ ! -f /etc/apt/sources.list.d/grafana.list ]] || \
        [[ "$(cat /etc/apt/sources.list.d/grafana.list)" != "$repository" ]]; then
        printf '%s\n' "$repository" >/etc/apt/sources.list.d/grafana.list
        repository_changed=true
        mark_ops_change
    fi
    chown root:root /etc/apt/sources.list.d/grafana.list
    chmod 0644 /etc/apt/sources.list.d/grafana.list
    if [[ "$repository_changed" == true ]] || ((${#missing_packages[@]} > 0)); then
        apt_get_retry update
    fi
    if ((${#missing_packages[@]} > 0)); then
        apt_get_retry install -y --no-install-recommends "${missing_packages[@]}"
    fi
    install_grafana_sqlite_plugin

    chown root:root /etc/hermes-grafana.env
    chmod 0600 /etc/hermes-grafana.env

    return
}

package_installed() {
    [[ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" == 'install ok installed' ]]
}

install_grafana_sqlite_plugin() {
    # Grafana reads route analytics straight from the SQLite snapshot; the
    # datasource plugin is pinned in vps-defaults.yml and installed idempotently.
    local plugin_version
    plugin_version="$(managed_value vps_observability.grafana.sqlite_plugin_version)"
    [[ "${plugin_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid Grafana SQLite plugin version in vps-defaults.yml"
    if grafana_plugins ls 2>/dev/null | grep -q "^frser-sqlite-datasource @ ${plugin_version}$"; then
        return
    fi
    log "installing Grafana SQLite datasource plugin ${plugin_version}"
    mark_ops_change grafana-server.service
    grafana_plugins install frser-sqlite-datasource "${plugin_version}"
}

grafana_plugins() {
    # Grafana 11+ deprecates the standalone grafana-cli binary and may return
    # non-zero after printing only its deprecation warning. Prefer the bundled
    # subcommand and retain the old binary solely for older distributions.
    if command -v grafana >/dev/null 2>&1 && grafana cli plugins --help >/dev/null 2>&1; then
        grafana cli plugins "$@"
        return
    fi
    command -v grafana-cli >/dev/null 2>&1 || die "Grafana CLI is unavailable"
    grafana-cli plugins "$@"
}

install_grafana_password_file() {
    local grafana_admin_user
    if [[ -f /etc/hermes-grafana.env ]]; then
        log "preserving existing Grafana administrator credentials"
        return
    fi
    command -v openssl >/dev/null 2>&1 || die "openssl is required to create the Grafana administrator password"
    grafana_admin_user="$(managed_value vps_observability.grafana.admin_user)"
    [[ "${grafana_admin_user}" =~ ^[A-Za-z0-9._-]{1,64}$ ]] ||
        die "invalid Grafana administrator user in vps-defaults.yml"
    mark_ops_change grafana-server.service
    umask 077
    printf 'GF_SECURITY_ADMIN_USER=%s\nGF_SECURITY_ADMIN_PASSWORD=%s\n' \
        "${grafana_admin_user}" "$(openssl rand -base64 36 | tr -d '\n')" \
        >/etc/hermes-grafana.env
    chown root:root /etc/hermes-grafana.env
    chmod 0600 /etc/hermes-grafana.env
    log "created Grafana administrator credentials in /etc/hermes-grafana.env (password not printed)"
}

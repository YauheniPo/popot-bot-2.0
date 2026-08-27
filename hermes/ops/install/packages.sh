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
    local grafana_channel
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
    apt_get_retry update
    apt_get_retry install -y --no-install-recommends openssl
    # Prepare Grafana's password and unit override before package installation:
    # the package manager may otherwise start Grafana with its default admin
    # account before our first-run credentials are visible.
    install_grafana_password_file
    install -d -o root -g root -m 0755 /etc/systemd/system/grafana-server.service.d
    render \
        "${SCRIPT_DIR}/systemd/grafana-hermes-loopback.conf" /etc/systemd/system/grafana-server.service.d/hermes-loopback.conf 0644
    systemctl daemon-reload
    # Prevent package post-install scripts from exposing the distribution
    # Prometheus listeners before Hermes installs its loopback-only units.
    # These distribution units remain intentionally masked afterwards.
    systemctl stop prometheus.service prometheus-node-exporter.service 2>/dev/null || true
    systemctl mask prometheus.service prometheus-node-exporter.service
    curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
        https://apt.grafana.com/gpg-full.key --output /etc/apt/keyrings/grafana.asc
    chmod 0644 /etc/apt/keyrings/grafana.asc
    printf 'deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.grafana.com %s main\n' "${grafana_channel}" \
        >/etc/apt/sources.list.d/grafana.list
    apt_get_retry update
    apt_get_retry install -y --no-install-recommends \
        grafana prometheus prometheus-node-exporter

    chown root:root /etc/hermes-grafana.env
    chmod 0600 /etc/hermes-grafana.env

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
    umask 077
    printf 'GF_SECURITY_ADMIN_USER=%s\nGF_SECURITY_ADMIN_PASSWORD=%s\n' \
        "${grafana_admin_user}" "$(openssl rand -base64 36 | tr -d '\n')" \
        >/etc/hermes-grafana.env
    chown root:root /etc/hermes-grafana.env
    chmod 0600 /etc/hermes-grafana.env
    log "created Grafana administrator credentials in /etc/hermes-grafana.env (password not printed)"
}

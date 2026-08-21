# shellcheck shell=bash

install_observability_dependencies() {
    command -v apt-get >/dev/null 2>&1 || die "Grafana observability requires apt-get on Debian/Ubuntu"
    command -v curl >/dev/null 2>&1 || die "curl is required to install the official Grafana package repository"

    log "installing Prometheus, node exporter, and Grafana"
    install -d -o root -g root -m 0755 /etc/apt/keyrings
    # `openssl` is needed only for the direct-deploy fallback password. Install
    # it before creating that file; Ansible normally creates the file from Vault.
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssl
    # Prepare Grafana's password and unit override before package installation:
    # the package manager may otherwise start Grafana with its default admin
    # account before our first-run credentials are visible.
    install_grafana_password_file
    install -d -o root -g root -m 0755 /etc/systemd/system/grafana-server.service.d
    install -o root -g root -m 0644 \
        "${SCRIPT_DIR}/systemd/grafana-hermes-loopback.conf" /etc/systemd/system/grafana-server.service.d/hermes-loopback.conf
    systemctl daemon-reload
    # Prevent package post-install scripts from exposing the distribution
    # Prometheus listeners before Hermes installs its loopback-only units.
    # These distribution units remain intentionally masked afterwards.
    systemctl stop prometheus.service prometheus-node-exporter.service 2>/dev/null || true
    systemctl mask prometheus.service prometheus-node-exporter.service
    curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
        https://apt.grafana.com/gpg-full.key --output /etc/apt/keyrings/grafana.asc
    chmod 0644 /etc/apt/keyrings/grafana.asc
    printf '%s\n' 'deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.grafana.com stable main' \
        >/etc/apt/sources.list.d/grafana.list
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        grafana prometheus prometheus-node-exporter

    chown root:root /etc/hermes-grafana.env
    chmod 0600 /etc/hermes-grafana.env

}

install_grafana_password_file() {
    if [[ -f /etc/hermes-grafana.env ]]; then
        log "preserving existing Grafana administrator credentials"
        return
    fi
    command -v openssl >/dev/null 2>&1 || die "openssl is required to create the Grafana administrator password"
    umask 077
    printf 'GF_SECURITY_ADMIN_USER=hermes\nGF_SECURITY_ADMIN_PASSWORD=%s\n' \
        "$(openssl rand -base64 36 | tr -d '\n')" >/etc/hermes-grafana.env
    chown root:root /etc/hermes-grafana.env
    chmod 0600 /etc/hermes-grafana.env
    log "created Grafana administrator credentials in /etc/hermes-grafana.env (password not printed)"
}



# shellcheck shell=bash

reconcile_observability_services() {
    local grafana_address
    local grafana_port
    local observability_output
    local timers_output
    local gateway_service
    local -a observability_services=()
    local -a timer_services=()

    observability_output="$(managed_services observability)"
    timers_output="$(managed_services timers)"
    gateway_service="$(managed_services gateway)"
    local service
    while IFS= read -r service; do observability_services+=("$service"); done <<< "$observability_output"
    while IFS= read -r service; do timer_services+=("$service"); done <<< "$timers_output"

    systemctl daemon-reload
    systemctl enable hermes-startup-notify.service
    log "enabling the loopback-only Prometheus and Grafana services"
    systemctl enable --now "${observability_services[@]}"
    if [[ "${START_TIMERS}" == "true" ]]; then
        if ! systemctl is-enabled --quiet hermes-backup.timer; then
            log "creating and verifying the initial scheduled backup"
            systemctl start hermes-backup.service
        fi
        systemctl enable --now "${timer_services[@]}"
    else
        log "timers installed but deferred"
    fi

    if [[ "${RESTART_SERVICES}" == true ]]; then
        for service in "${observability_services[@]}"; do
            if [[ -f "$OPS_RESTART_DIR/$service" ]]; then
                systemctl restart "$service"
                rm -f -- "$OPS_RESTART_DIR/$service"
            fi
        done
        if [[ "${START_TIMERS}" == "true" ]]; then
            for service in "${timer_services[@]}"; do
                if [[ -f "$OPS_RESTART_DIR/$service" ]]; then
                    systemctl restart "$service"
                    rm -f -- "$OPS_RESTART_DIR/$service"
                fi
            done
        fi
        if [[ -f "$OPS_RESTART_DIR/$gateway_service" ]] && \
            systemctl is-active --quiet "${gateway_service}"; then
            log "restarting the gateway so it loads the changed observability plugin"
            systemctl restart "${gateway_service}"
            rm -f -- "$OPS_RESTART_DIR/$gateway_service"
        fi
    fi

    log "installed successfully"
    log "report: sudo -u ${HERMES_USER} HERMES_HOME=${HERMES_HOME} hermes-ops-report --period 24h"
    log "timers: systemctl list-timers 'hermes-*'"
    grafana_address="$(managed_value vps_observability.grafana.bind_address)"
    grafana_port="$(managed_value vps_observability.grafana.port)"
    log "Grafana: http://${grafana_address}:${grafana_port} (credentials are in /etc/hermes-grafana.env)"
    return
}

# shellcheck shell=bash

SUPERPOWERS_REPO="https://github.com/obra/superpowers.git"
SUPERPOWERS_REF="v6.3.0"

install_superpowers_plugin() {
    log "installing the superpowers skills plugin (flattened layout)"
    install -d -o "${HERMES_USER}" -g "${HERMES_GROUP}" -m 0700 \
        "${HERMES_HOME}/plugins/superpowers"

    local tmp
    tmp="$(mktemp -d /tmp/superpowers-clone.XXXXXX)"
    if ! git clone --depth 1 --branch "${SUPERPOWERS_REF}" \
        "${SUPERPOWERS_REPO}" "${tmp}/repo" >/dev/null 2>&1; then
        log "WARNING: could not clone ${SUPERPOWERS_REPO} (${SUPERPOWERS_REF}); skipping superpowers install"
        rm -rf -- "${tmp}"
        return 0
    fi

    # Flattened layout: the .hermes-plugin manifest files land at the plugin
    # root and skills/ sits next to them, matching the layout the plugin's
    # register() resolves. The full repo cannot go through `hermes plugins
    # install` because its documentation mentions CLAUDE.md/AGENTS.md, which
    # the security scanner flags as critical persistence false positives.
    install -o "${HERMES_USER}" -g "${HERMES_GROUP}" -m 0600 \
        "${tmp}/repo/.hermes-plugin/plugin.yaml" \
        "${tmp}/repo/.hermes-plugin/__init__.py" \
        "${HERMES_HOME}/plugins/superpowers/"
    local skills_stage
    skills_stage="$(mktemp -d "${HERMES_HOME}/plugins/superpowers/.skills-stage.XXXXXX")"
    cp -r "${tmp}/repo/skills" "${skills_stage}/skills"
    rm -rf -- "${HERMES_HOME}/plugins/superpowers/skills"
    mv "${skills_stage}/skills" "${HERMES_HOME}/plugins/superpowers/skills"
    chown -R "${HERMES_USER}:${HERMES_GROUP}" \
        "${HERMES_HOME}/plugins/superpowers/skills"
    chmod -R go-rwx "${HERMES_HOME}/plugins/superpowers/skills"
    rm -rf -- "${skills_stage}"
    rm -rf -- "${tmp}"

    if [[ -x "${HERMES_BIN}" ]]; then
        log "validating and enabling the superpowers plugin"
        if ! timeout --foreground 60s runuser -u "${HERMES_USER}" -- env -i \
            HOME="${USER_HOME}" \
            HERMES_HOME="${HERMES_HOME}" \
            PATH="${USER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin" \
            "${HERMES_BIN}" plugins doctor "${HERMES_HOME}/plugins/superpowers" --ci </dev/null; then
            log "WARNING: superpowers plugin validation timed out or failed"
        fi
        enable_superpowers_plugin
    else
        log "Hermes CLI not found yet; enable later by adding superpowers to plugins.enabled in config.yaml"
    fi
}

enable_superpowers_plugin() {
    local hermes_python="${HERMES_HOME}/hermes-agent/venv/bin/python"
    local result
    [[ -x "${hermes_python}" ]] || {
        log "WARNING: Hermes Python environment is unavailable; the superpowers plugin was not enabled"
        return 0
    }

    result="$(runuser -u "${HERMES_USER}" -- env -i \
        HOME="${USER_HOME}" \
        HERMES_HOME="${HERMES_HOME}" \
        PATH="${USER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin" \
        "${hermes_python}" "${SCRIPT_DIR}/configure-superpowers.py" \
        --config "${HERMES_HOME}/config.yaml")"
    if [[ "${result}" == "changed" ]]; then
        PLUGIN_CONFIGURATION_CHANGED=true
    fi
}

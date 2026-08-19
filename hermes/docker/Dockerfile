ARG HERMES_IMAGE=nousresearch/hermes-agent:latest
FROM ${HERMES_IMAGE}

USER root

# HTTPS GitHub operations and pull-request workflows use the official GitHub
# CLI. Authentication itself remains runtime-only through GITHUB_TOKEN in the
# ignored docker/local.env file; no credential is baked into this image.
RUN apt-get update \
 && apt-get install --no-install-recommends -y gh sudo \
 && printf 'hermes ALL=(ALL:ALL) NOPASSWD: ALL\n' >/etc/sudoers.d/hermes-container-admin \
 && chmod 0440 /etc/sudoers.d/hermes-container-admin \
 && rm -rf /var/lib/apt/lists/*

# The upstream entrypoint remains intact. This cont-init hook runs after its
# volume setup and before Hermes starts, so the agent still runs as the
# unprivileged `hermes` user.
COPY --chmod=0755 hermes/docker/20-local-bootstrap /etc/cont-init.d/20-local-bootstrap
COPY --chmod=0555 hermes/ops/apply-edge-tts-retry.py /opt/hermes-local/apply-edge-tts-retry.py
COPY --chmod=0555 hermes/docker/start-local.sh /opt/hermes-local/start-local.sh
COPY --chmod=0555 hermes/docker/export-vps-config.py /opt/hermes-local/export-vps-config.py
COPY --chmod=0555 hermes/ops/export-metrics.py /opt/hermes-local/export-metrics.py
COPY --chmod=0644 hermes/docker/AGENTS.md /opt/hermes-local/container-admin-AGENTS.md

# Version the plugins under test into the image. The bootstrap hook copies them
# into the isolated Hermes data volume on every container start.
COPY --chmod=0644 hermes/ops/plugin/ops-observability/ \
  /opt/hermes-local/ops-observability/
COPY --chmod=0644 hermes/ops/templates/model-prices.json \
  /opt/hermes-local/model-prices.json

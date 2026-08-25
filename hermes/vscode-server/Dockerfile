FROM codercom/code-server:latest

USER root

ARG HERMES_UID=1000
ARG HERMES_GID=1000

# git and gh are needed for repository work inside the IDE; gh doubles as the
# Git credential helper via GIT_CONFIG_* env vars from docker-compose.yml.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git gh python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Match bind-mounted file ownership even when the host Hermes account is not
# UID/GID 1000. Keep the image user name so the upstream entrypoint continues
# to initialize code-server normally.
RUN current_uid="$(id -u coder)" \
    && current_gid="$(id -g coder)" \
    && if [ "${current_gid}" != "${HERMES_GID}" ]; then groupmod -o -g "${HERMES_GID}" coder; fi \
    && if [ "${current_uid}" != "${HERMES_UID}" ]; then usermod -o -u "${HERMES_UID}" coder; fi \
    && usermod -g "${HERMES_GID}" coder \
    && install -d -o "${HERMES_UID}" -g "${HERMES_GID}" \
        /home/coder/.local/bin /home/coder/workspace/repositories \
    && chown -R "${HERMES_UID}:${HERMES_GID}" /home/coder

USER coder

# Open straight into the shared repositories workspace.
WORKDIR /home/coder/workspace/repositories

EXPOSE 8080

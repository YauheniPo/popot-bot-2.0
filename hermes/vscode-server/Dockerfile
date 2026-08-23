FROM codercom/code-server:latest

USER root

# git and gh are needed for repository work inside the IDE; gh doubles as the
# Git credential helper via GIT_CONFIG_* env vars from docker-compose.yml.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git gh python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

USER coder

# Open straight into the shared repositories workspace.
WORKDIR /home/coder/workspace/repositories

EXPOSE 8080
#!/usr/bin/env bash
# Code-server deploy script for hermes VPS
# Usage: ./deploy.sh [port]
# Default: binds localhost:8080 -> container:3000

set -euo pipefail

VPS_USER="${HERMES_USER:-hermes}"
VPS_HOME="/home/${VPS_USER}"
HERMES_AGENT_DIR="${VPS_HOME}/.hermes/hermes-agent"
VPS_HOST="${1:-localhost}"

# Default SSH tunnel port mapping: local 8080 -> container 3000
TUNNEL_LOCAL_PORT="${2:-8080}"
TUNNEL_REMOTE_PORT="${3:-3000}"

echo "=== Code-server Deploy Script ==="
echo "User: ${VPS_USER}"
echo "Home: ${VPS_HOME}"
echo "Tunnel: localhost:${TUNNEL_LOCAL_PORT} -> remote:${TUNNEL_REMOTE_PORT}"

# 1. Create code-server config directory
sudo -u "${VPS_USER}" mkdir -p "${VPS_HOME}/.config/code-server"

# 2. Write config.json
cat > "${VPS_HOME}/.config/code-server/config.json" << 'EOF'
{
  "bind-addr": "0.0.0.0:3000",
  "auth": "password",
  "password": "",
  "file-mounts": {
    "/home/${VPS_USER}/workspace/repositories": "/home/coder/repositories",
    "/home/${VPS_USER}/.hermes": "/home/coder/.hermes"
  },
  "max-http-sessions": 1,
  "cert-dir": "/home/${VPS_USER}/.local/share/code-server/Certs",
  "extensions-dir": "/home/${VPS_USER}/.local/share/code-server/extensions",
  "user-data-dir": "/home/${VPS_USER}/.local/share/code-server"
}
EOF

sudo -u "${VPS_USER}" chmod 600 "${VPS_HOME}/.config/code-server/config.json"

# 3. Create systemd service file
cat > "/etc/systemd/system/code-server.service" << 'EOF'
[Unit]
Description=code-server - The VS Code web IDE
After=network.target

[Service]
Type=simple
User=${VPS_USER}
ExecStart=/usr/local/bin/code-server --bind-addr 0.0.0.0:3000 --auth password --cert false --disable-telemetry
Restart=on-failure
RestartSec=5
WorkingDirectory=${VPS_HOME}

# Security hardening
ProtectSystem=strict
PrivateTmp=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF

# 4. Reload systemd and start service
sudo systemctl daemon-reload
sudo systemctl enable code-server.service
sudo systemctl start code-server.service

# 5. Verify service status
sleep 2
systemctl is-active code-server.service && echo "✓ code-server is running" || echo "✗ code-server failed to start"

# 6. Display SSH tunnel instructions
echo ""
echo "=== SSH Tunnel Instructions ==="
echo "To access Code-server from your browser:"
echo "  ssh -L ${TUNNEL_LOCAL_PORT}:localhost:${TUNNEL_REMOTE_PORT} ${VPS_USER}@VPS_IP"
echo ""
echo "Then open: http://localhost:${TUNNEL_LOCAL_PORT}"
echo ""
echo "=== Repositories Available ==="
echo "- ~/workspace/repositories (all repos)"
echo "- ~/.hermes (Hermes agent config)"
echo ""
echo "=== Management Commands ==="
echo "  systemctl status code-server    - Check status"
echo "  systemctl restart code-server   - Restart"
echo "  systemctl logs code-server      - View logs"
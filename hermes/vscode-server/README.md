# code-server on the Hermes VPS (Docker)

VS Code in your browser, running in Docker on the VPS. It opens the same
repositories directory that Hermes uses, so edits, git commits, and pushes
happen directly on the host files — no copies, no sync.

## What the container sees

| Host path | Container path | Mode | Purpose |
| --- | --- | --- | --- |
| `~/workspace/repositories` | `/home/coder/workspace/repositories` | read-write | all managed repos; edit + git here |
| `~/.hermes` | `/home/coder/hermes-home` | **read-write** | full access to configs and skills from the IDE |
| `~/.gitconfig` | `/home/coder/.gitconfig` | read-only | host Git identity and defaults |
| `~/.config/gh` | `/home/coder/gh-config` | read-write | GitHub CLI auth for push/pull (`GH_CONFIG_DIR`) |

Git identity comes from the shared `~/.gitconfig` (user YauheniPo). The
compose file overrides only the credential helper to use the container's own
`gh`, which reads the mounted auth state — no token is copied into the image.

The container runs as UID/GID 1000 (`hermes`), so every file created from the
browser IDE is owned by `hermes` on the host.

## Deploy

The Ansible playbook installs and starts code-server automatically
(`vps_deploy.features.vscode_server: true` in `hermes/config/vps-defaults.yml`).
Set the password once in encrypted `vault.yml`:

```yaml
hermes_code_server_password: "your-strong-password"
```

Manual deploy (without Ansible):

```bash
cd hermes/vscode-server
echo "PASSWORD=your-strong-password" > /etc/code-server.env  # root-only
sudo docker compose up -d --build
```

The codercom image reads the password from the `PASSWORD` variable
(`CODE_SERVER_PASSWORD` is ignored by code-server).

code-server binds to port **3001** on the VPS (loopback only; host 3000
is taken by Grafana) and is not published beyond it; access is SSH-tunnel-only:

```bash
ssh -L 8080:localhost:3001 hermes@VPS_IP
# then open http://localhost:8080
```

## Security model

- Nothing is exposed publicly: port 3001 is reachable only through the SSH tunnel.
- Browser access = SSH key + code-server password (from encrypted vault.yml).
- `.hermes` is mounted read-write per owner choice: the IDE can edit gateway
  configs directly. Treat browser edits with the same care as SSH edits.
- `no-new-privileges` is enabled on the container.

## Caveat: restarting Hermes after config edits

The container shares files but not the host systemd session. After editing
`config.yaml` or other runtime state from the IDE, apply changes on the VPS as
usual (for example, restart `hermes-gateway.service`) — editing a file in the
browser does not reload the running gateway by itself.

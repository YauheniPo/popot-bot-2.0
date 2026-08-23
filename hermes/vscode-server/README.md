# code-server on the Hermes VPS (Docker)

VS Code in your browser, running in Docker on the VPS. It opens the same
repositories directory that Hermes uses, so edits, git commits, and pushes
happen directly on the host files — no copies, no sync.

## What the container sees

| Host path | Container path | Mode | Purpose |
| --- | --- | --- | --- |
| `~/workspace/repositories` | `/home/coder/workspace/repositories` | read-write | all managed repos; edit + git here |
| `~/.hermes` | `/home/coder/hermes-home` | **read-only** | browse configs/skills; cannot mutate gateway state |
| `~/.gitconfig` | `/home/coder/.gitconfig` | read-only | host Git identity and defaults |
| `~/.config/gh` | `/home/coder/.config/gh` | read-write | GitHub CLI auth for push/pull |

Git identity comes from the shared `~/.gitconfig` (user YauheniPo). The
compose file overrides only the credential helper to use the container's own
`gh`, which reads the mounted auth state — no token is copied into the image.

The container runs as UID/GID 1000 (`hermes`), so every file created from the
browser IDE is owned by `hermes` on the host.

## Deploy

```bash
cd hermes/vscode-server
sudo docker compose up -d --build
```

code-server binds to port 3000 on the VPS loopback network namespace of the
host but is not published beyond it; access is SSH-tunnel-only:

```bash
ssh -L 8080:localhost:3000 hermes@VPS_IP
# then open http://localhost:8080
```

Get the one-time password from the container logs:

```bash
sudo docker compose logs code-server | grep -A1 "Password"
```

Or set a fixed password before starting:

```bash
echo "DOCKER_PASSWORD=your-strong-password" > .env   # not committed
```

## Security model

- Nothing is exposed publicly: port 3000 is reachable only through the SSH tunnel.
- Browser access = SSH key + code-server password.
- `.hermes` is mounted read-only so a browser tab cannot alter gateway state,
  sessions, or secrets.
- `no-new-privileges` is enabled on the container.

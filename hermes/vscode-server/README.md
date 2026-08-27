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
| `../runtime/github-cli-wrapper.py` | `/home/coder/.local/bin/gh` | read-only | managed GitHub credential helper |

Git identity comes from the shared `~/.gitconfig`. The Compose environment
resets its host-only helper path and uses the mounted managed `gh` wrapper,
which reads the token from the mounted Hermes home without copying it into the
image, Compose file, or GitHub CLI config.

Therefore the integrated terminal and Source Control view operate on the same
working trees as Hermes: manual `git diff`, commits, pulls, and pushes use the
configured author identity and the same managed GitHub account as the agent.
Ansible verifies both the Git identity and authenticated GitHub login after
starting the container.

When code-server is enabled, Hermes also registers `/docker_restart` against
the managed `/opt/hermes-bootstrap` Compose project. Typing that slash command
is an explicit owner action. If the agent itself needs to restart code-server
or another system service from the terminal, the managed
`approvals.mode: manual` policy requests owner approval first.

The image builds its `coder` account with the actual host UID/GID of `hermes`,
so files created from the browser IDE keep the correct host ownership even
when that account is not UID/GID 1000.

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
read -rsp 'code-server password: ' CODE_SERVER_PASSWORD
printf '\n'
if [[ ! "$CODE_SERVER_PASSWORD" =~ ^[A-Za-z0-9._~!@%^+=:,-]{16,128}$ ]]; then
  echo 'Use 16-128 characters: letters, digits, . _ ~ ! @ % ^ + = : , -' >&2
  exit 1
fi
mkdir -p "$HOME/workspace/repositories"
touch "$HOME/.gitconfig"
HERMES_UID="$(id -u)"
HERMES_GID="$(id -g)"
printf '%s\n' \
  "PASSWORD=$CODE_SERVER_PASSWORD" \
  "VPS_USER_HOME=$HOME" \
  "VPS_HERMES_HOME=$HOME/.hermes" \
  "HERMES_UID=$HERMES_UID" \
  "HERMES_GID=$HERMES_GID" \
  "CODE_SERVER_REPOSITORIES_DIR=$HOME/workspace/repositories" \
  "CODE_SERVER_PROJECT_NAME=hermes-vscode" \
  "CODE_SERVER_IMAGE=codercom/code-server:4.133.0-noble@sha256:c8ae938c488efc7f346deb93c04c11320b803251aa181263a8482f3aeddf1b27" \
  "CODE_SERVER_BIND_ADDRESS=127.0.0.1" \
  "CODE_SERVER_HOST_PORT=3001" \
  "CODE_SERVER_TIMEZONE=UTC" | \
  sudo install -o root -g root -m 0600 /dev/stdin /etc/code-server.env
unset CODE_SERVER_PASSWORD
sudo docker compose --project-name hermes-vscode \
  --env-file /etc/code-server.env up -d --build
```

The codercom image reads the password from the `PASSWORD` variable
(`CODE_SERVER_PASSWORD` is ignored by code-server). Ansible takes the pinned
image, bind address, port and project name from `hermes/config/vps-defaults.yml`.

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
- The password is validated before deployment and stored root-only with mode `0600`.
- `no-new-privileges` is enabled on the container.

## Caveat: restarting Hermes after config edits

The container shares files but not the host systemd session. After editing
`config.yaml` or other runtime state from the IDE, apply changes on the VPS as
usual (for example, restart `hermes-gateway.service`) — editing a file in the
browser does not reload the running gateway by itself.

# Hermes security boundaries

These notes describe repository configuration, not a verification of a running
VPS or container. They do not dismiss or suppress scanner alerts.

## Local maintenance tools

- `configure-plugin.py` validates command arguments at the construction boundary,
  including when called from Python. Configurable paths are intentional: this is
  an install-time administration tool, not a sandbox for an untrusted operator.
- `apply-edge-tts-retry.py` accepts only the installed TTS target or a target under
  the configured Hermes roots. It refuses hardlinks and opens the resolved file
  without following a newly substituted leaf symlink. Read and write use the same
  descriptor. Ansible runs it as the Hermes service account, not root. The roots
  and their parent directories must remain controlled by the operator.
- `prune-observability.py` operates on the canonical managed metrics path. SQLite
  receives an encoded file URI in `mode=rw`, so filename characters cannot become
  connection options and a disappeared database is not silently recreated.
- Workspace `AGENTS.md` may include private operator notes; deployment writers
  use `0600`. The host `/etc/code-server` directory is root-only (`0700`); it is
  not the container's separately mounted configuration directory.

## Alerts requiring context or an operator decision

| GitHub alerts | Current boundary and disposition |
|---|---|
| #5, #6: Tailscale files readable by others | The signing key is public, not private. The APT source list is public metadata. Root owns both with `0644`; unprivileged APT readers need access. Do not change these to `0600` merely to clear an alert. |
| #16–#18: HTTP links in deployment output | The links describe loopback-only monitoring endpoints. Remote access must use an authenticated encrypted SSH/Tailscale tunnel. A printed HTTPS link does not enable TLS. |
| #19: HTTP retry endpoint | The validator accepts only `http://127.0.0.1:...`, not arbitrary remote hosts. External exposure requires a separate authenticated TLS design. |
| #15: Docker root user | Root is used by the pinned image's initialization. The local administration profile also intentionally permits passwordless sudo. This is a real privilege trade-off, not proof that the agent is isolated from container root. Removing it requires changing the profile and testing initialization and the supported administration workflow. |

The local Compose topology does not grant `privileged` access or mount the Docker
socket into the agent container. This does not make container root harmless.
No claim about the effective runtime UID, host firewall, or tunnel configuration
can be established by reviewing these source files alone.

Rerun analysis after merging changes before treating any alert as fixed. Review
justified alerts in SonarCloud explicitly; do not weaken Quality Gate thresholds
or add blanket exclusions to hide them.

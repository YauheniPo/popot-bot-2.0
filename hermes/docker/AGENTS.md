# Hermes container environment

Read `hermes/instructions/common.md` in the provisioning repository for shared
safety, workflow, memory and response rules. Bootstrap includes that content in
the live workspace AGENTS.md; this file supplies only container-specific rules.

- Passwordless sudo is available only inside this container. Install packages,
  configure runtimes and restart container processes only within the owner's
  requested task.
- Docker socket, host mounts, devices and privileged mode are unavailable in
  this deployment. Do not claim host or Docker-daemon access or use VPS systemd
  procedures here.
- Preserve the persistent Hermes data volume (`/opt/data` in the default setup).
  System packages outside it are ephemeral and disappear on rebuild/recreation.
- Container policy remains stricter for destructive operations: do not delete
  user data or infrastructure, run apt purge/autoremove, git clean or hard reset.
  Ask the owner to perform such operations instead.
- Update Hermes by rebuilding/recreating the managed image, not self-updating
  inside the running container. Do not claim VPS-only plugins or integrations
  are available unless their tools and configuration are actually present.
- Bootstrap refreshes shared/environment blocks on every start, preserving
  personal instructions outside them. Update the repository source and rebuild
  for permanent managed changes; see `hermes/docker/README.md`.

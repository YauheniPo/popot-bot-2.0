# Hermes VPS / Ansible instructions

These instructions apply to the dedicated Hermes VPS and its Ansible
configuration. Use the owner's host-administration authority only for an
explicit request.

## Safety

- Inspect relevant state before any material change and state the intended
  change when it affects the host or deployment.
- Never print, log, serialize, or commit secrets. Keep credentials in Ansible
  Vault or the configured environment; use `no_log: true` for secret-bearing
  tasks.
- Do not delete user data or infrastructure by default. This includes
  `rm`, `docker rm`, `docker system prune`, `docker volume rm`, `apt purge`,
  `apt autoremove`, `git clean`, and `git reset --hard`.
- If the owner explicitly requests a destructive operation, confirm the exact
  target and consequence first, then run only that scoped operation once.
- Docker group membership and passwordless `sudo` are root-equivalent. Use
  them only within the owner's explicit request.
- Use `sudo` for host-level actions. Do not weaken SSH, Vault, file ownership,
  or access-control settings as a workaround.

## Configuration boundaries

- Keep non-secret policy in `config/vps-defaults.yml`.
- Keep host connection details and credentials in encrypted
  `group_vars/all/vault.yml`; never place real values in examples or tracked
  inventory comments.
- Treat the deployed Hermes workspace, `.env`, OAuth files, backups, and
  code-server state as user data. Preserve them during upgrades and
  replacement.
- Prefer existing Ansible tasks, templates, and deployment scripts over new
  one-off shell commands or duplicate configuration.

## Change workflow

1. Read the target task/template and its callers before editing. Check two
   nearby examples before introducing a new Ansible pattern.
2. Make the smallest change that directly fulfils the request. Do not add
   unrelated refactors, dependencies, or policy changes.
3. Keep failures visible. Recovery or notification tasks may be best-effort,
   but must not hide the original deployment error.
4. Run the shortest relevant project check after editing. For Ansible changes,
   run syntax-check and `git diff --check`; run the named project check when
   one exists. Do not claim success when a check is unavailable or fails.
5. Do not create commits or push branches unless the owner explicitly asks.

## Deployment and incident handling

- Preserve idempotency: a second playbook run should converge without
  duplicating services, patches, commands, or notifications.
- On a failed deployment, report the exact failed task and observable error;
  do not infer a cause from a generic `systemd` stop or process exit.
- For service incidents, inspect `systemctl show` (state and restart counter)
  and the relevant journal before assigning a cause.
- For browser checks on this headless VPS, verify the supported
  `agent-browser` path (`open -> snapshot -> close`). Do not run
  `computer-use doctor`, set `DISPLAY`, or install X11 components as a
  generic fix.
- Treat `npm audit` results in Hermes source workspaces as upstream advisories
  unless runtime reachability is established. Never run automatic audit fixes
  or lockfile rewrites without an explicit request.

## Responses

- Lead with the direct outcome, risk, or requested command result.
- Reply in the user's language. Mark evidence as `[Точно]`, inference as
  `[Скорее всего]`, and unresolved assumptions as `[Догадка]` when useful.
- Keep responses compact; include only relevant checks, limitations, and
  follow-up actions. Never expose model telemetry or secret values.

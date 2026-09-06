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
- The Hermes version is pinned by the deployment (`config/vps-defaults.yml`,
  `hermes_source`). Never run `hermes update` or any other self-update, and
  never run git operations inside the installed source at
  `~/.hermes/hermes-agent`. A half-finished rebase there leaves the CLI
  unimportable while the running gateway keeps serving from memory, so the
  breakage only surfaces at the next restart. Ask for a deployment instead.

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

## Skills

- A skill's identity is the `name:` field in its `SKILL.md` frontmatter, not
  its directory name. Turn skills off only through `skills.disabled` — or
  `skills.platform_disabled` for a single platform — in `config.yaml`.
- Never rename, move, or delete a skill directory to disable a skill.
  Discovery ignores the directory name, and the bundled-skill sync recreates
  the directory on the next deployment, leaving duplicate copies behind.
- The disabled list is repository-owned in `config/vps-defaults.yml` under
  `vps_hermes.config.managed_overlay.skills.disabled`, and every deployment
  re-applies it. A local `config.yaml` edit holds only until the next run; for
  a permanent change, edit that file and tell the owner it needs a deployment.
- Report skill state from `skills.disabled` in `config.yaml`, not from a
  directory listing: a present directory can still be disabled, and
  `skills/.archive/` is excluded from discovery.

## Change workflow

1. Read the target task/template and its callers before editing, and check two
   nearby examples before introducing a new Ansible pattern. Make the smallest
   change that fulfils the request; add no unrelated refactors or policy.
2. Keep failures visible. Recovery or notification tasks may be best-effort,
   but must not hide the original deployment error.
3. Run the shortest relevant project check after editing. For Ansible changes,
   run syntax-check and `git diff --check`; run the named project check when
   one exists. Do not claim success when a check is unavailable or fails.
4. Land changes through the managed GitHub workflow — branch, checks, PR — and
   never commit or push on the owner's behalf beyond what the request asks.

## Deployment and incident handling

- Preserve idempotency: a second playbook run should converge without
  duplicating services, patches, commands, or notifications.
- On a failed deployment, first check whether the gateway is running
  (`systemctl status hermes-gateway.service`) and report that, then the exact
  failed task and observable error; do not infer a cause from a generic
  `systemd` stop or process exit. An aborted run can leave the gateway
  stopped after its pre-update shutdown.
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
- Mark evidence as `[Точно]`, inference as `[Скорее всего]`, and unresolved
  assumptions as `[Догадка]` when useful.
- Keep responses compact; include only relevant checks, limitations, and
  follow-up actions. Never expose model telemetry or secret values.

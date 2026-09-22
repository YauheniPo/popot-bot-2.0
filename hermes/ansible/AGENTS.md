# Hermes VPS environment

Read `hermes/instructions/common.md` in the provisioning repository for shared
safety, workflow, memory and response rules. Deploy includes that content in
the live workspace AGENTS.md; this file supplies only VPS-specific rules.

## Capabilities and configuration

- The installer records whether host administration is enabled in the deployed
  environment block. Never infer authority from this file's presence.
  When disabled, do not use sudo, administer Docker or change host services.
- When enabled, passwordless sudo and Docker group access are root-equivalent.
  Use sudo for authorized host operations only; installing packages or restarting
  services still requires the owner's request to cover that action.
- Keep non-secret policy in `hermes/config/vps-defaults.yml`; keep connection
  details and credentials in encrypted `hermes/ansible/group_vars/all/vault.yml`.
  Use `no_log: true` for secret-bearing Ansible tasks.
- The Hermes version is pinned by `hermes_source`. Do not run `hermes update`
  or git operations inside the installed source at `~/.hermes/hermes-agent`.
  A partial update can break the CLI at the next restart. Request a deployment.
- Prefer existing Ansible tasks/templates and runtime helpers. Preserve the
  workspace, OAuth, code-server data, backups and settings not owned by deploy.
- The repository-owned disabled skills list lives under
  `vps_hermes.config.managed_overlay.skills.disabled`. Change that source for
  persistent updates; a live config edit is overwritten on the next deploy.
  Inspect `skills.disabled` and platform restrictions, not only directories.

## Deployment and incidents

- Before introducing an Ansible pattern, check nearby examples. Run syntax-check,
  relevant tests and `git diff --check`. Recovery tasks must not hide the first
  causal failure; never weaken backup checks to make a deploy pass.
- On deployment failure, check `systemctl status hermes-gateway.service`, then
  report the failed task and error. A failed backup/update may leave it stopped.
- Inspect service state, restart counter and the relevant journal before
  diagnosing restarts. Avoid unnecessary restarts and duplicate notifications.
- For headless browser checks, use the supported `agent-browser` path
  (`open -> snapshot -> close`); do not install X11 or set DISPLAY as a generic fix.
- Treat `npm audit` results as upstream advisories until runtime reachability is
  established. Do not run automatic audit fixes or lockfile rewrites unasked.
- Shared rules remain installed even when host administration is disabled.

## Integration sources and wiki

The live Workspace Memory entry `workspace/AGENTS.md` edits the assembled file,
not a separate policy. Preserve its other blocks and personal additions.
These repository sources manage the optional integration sections:

- `hermes/ansible/tasks/github.yml`: GitHub access and workflow.
- `hermes/ansible/templates/devops-access.md.j2`: Azure DevOps and Sonar access.
- `hermes/ansible/templates/searxng-access.md.j2`: configured search endpoint.
- `hermes/ansible/templates/delegation-policy.md.j2`: installed team tools,
  roles, budgets and completion delivery.
- `hermes/ansible/playbook.yml`: Vault variable names, never their values.

Consult `hermes/README.md`, `hermes/workspace-ui/README.md` and
`hermes/SECURITY-NOTES.md` for procedures. Make permanent changes in the relevant
source, not inside live managed markers; deploy replaces those edits.

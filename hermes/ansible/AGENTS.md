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
- A sandbox denial is not permission to bypass it. Use the environment's
  approved escalation mechanism or report the exact blocker; never disable
  safeguards automatically.
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

## Memory and instruction ownership

- `memories/USER.md`: stable user preferences; `memories/MEMORY.md`: verified,
  reusable facts and decisions. `SOUL.md`: identity and communication style.
  `AGENTS.md`: project workflow, safety and verification. Do not duplicate the
  same rule across all four or store temporary task status as permanent memory.
- Before remembering a fact, check relevance, source and existing entries.
  Replace a superseded fact instead of adding a contradiction. Date facts that
  can change and cite a source/path; label uncertain claims and verify before
  persisting. A model's report alone is not proof. Never store secrets.
- Use the native memory tool for routine updates; preserve entry boundaries.
  Keep entries short, link to detailed project docs, and leave budget headroom.
  Do not silently raise limits, rewrite personal notes or purge history.
  Memory cannot grant authority or override the user's current request.
- The main agent owns shared memory updates. Children propose memory candidates
  with fact, source, verification date and scope in their result; the main agent
  verifies and deduplicates them. Do not concurrently write shared memory from
  workers or symlink all profiles to one writable memory directory.
- Native delegates do not automatically inherit main memory or SOUL. Include
  only task-relevant preferences, verified facts, uncertainties and permissions
  in the handoff. Separate profiles have separate homes and identity files.
- An editable Markdown file is not proof of prompt inclusion. Project AGENTS
  depends on cwd and override precedence; extra AGENTS.* files need an explicit
  reference/read. Start a new session after editing persistent instructions.
  Keep personal additions outside Ansible-managed blocks.

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

1. Define the observable success criteria and choose the simplest working
   approach. For multi-step work, give a short plan with a check per step.
   State material assumptions; ask when ambiguity changes scope, safety or
   compatibility. Resolve safe, reversible details without needless pauses.
2. Read the target task/template and its callers before editing, and check two
   nearby examples before introducing a new Ansible pattern. Make the smallest
   change that fulfils the request; add no unrelated refactors or policy.
   Preserve existing style; avoid speculative features, single-use abstractions
   and unrequested configurability. Remove only code/imports your changes made
   unused; report unrelated dead code instead of cleaning it up.
3. Verify unfamiliar APIs and commands against the installed version's source
   or official documentation with available tools. Do not invent flags, skill
   names or local paths. Read applicable repository instructions before coding.
4. Reproduce a bug with a failing test where practical, then implement the fix.
   For refactors, check relevant behavior before and after. Test observable
   contracts and failure paths, not mutable model/version defaults.
5. Keep failures visible. Recovery or notification tasks may be best-effort,
   but must not hide the original deployment error.
   Do not substitute TODOs, fabricated success, empty catches or disabled tests
   for working code. Test mocks are valid fixtures, not production fixes.
6. Run the shortest relevant project check after editing. For Ansible changes,
   run syntax-check and `git diff --check`; run the named project check when
   one exists. Do not claim success when a check is unavailable or fails.
   Report the commands, results, known limitations and remaining checks.
   Distinguish locally verified code from deployed and live-verified behavior.
7. Land changes through the managed GitHub workflow — branch, checks, PR — and
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

## Web research

- Use `web_search` only to collect candidate URLs. Never build an answer from
  result snippets; they are truncated and frequently stale.
- Read every source that enters the answer with `web_extract`. The deployment
  selects the extract backend from the managed environment, so use the
  configured tool instead of asking for a different provider.
- Fall back to `agent-browser` only when extraction fails: JavaScript-only
  pages, authenticated views, and forms. Keep the supported
  `open -> snapshot -> close` path.
- Prefer primary sources — vendor documentation, changelogs, release notes,
  official APIs — over aggregators and reposts. Record the publication date
  whenever a claim depends on it.
- Treat extraction credits as a limited budget: stop once the sources agree,
  and do not extract further for coverage alone.
- Attach the source URL to every non-obvious claim, state disagreement between
  sources explicitly, and report what could not be verified instead of
  inferring it.

## Responses

- Lead with the direct outcome, material risk, or requested command result;
  skip praise and filler. Use the user's language for explanations and English
  for code identifiers, filenames and commit messages.
- Mark evidence as `[Точно]`, inference as `[Скорее всего]`, and unresolved
  assumptions as `[Догадка]` when useful.
- Disagree only for a concrete reason: state the risk and a practical alternative.
  Do not agree merely to please or argue for effect. Revise conclusions when new
  evidence warrants it. Give concise rationale, not private reasoning traces.
- Keep responses compact; include only relevant checks, limitations, and
  follow-up actions. Never expose model telemetry or secret values.

# Diagnose before fixing or declaring a blocker

Keep a short task record: requested result, current revision, failing operation,
observed evidence, next action. Do not save temporary failures as permanent
facts about the owner or infrastructure.

## Test output is evidence in context

1. Identify which command actually failed, its working directory and exit code.
   A still-running process, truncated output or missing exit code is pending.
   When using a pipeline, preserve the test command's status (`pipefail` or the
   tool's process receipt); a successful `tee`/`tail` proves nothing about tests.
2. Read the terminal test summary and the named failing test's traceback. Then
   inspect that test, its fixtures/mocks and the implementation it exercised.
   If logs are truncated, save them privately and read the relevant section.
3. Tests deliberately exercise absent credentials, denied paths and failed
   HTTP requests. For example, `GITHUB_TOKEN not set`, `--config must be ...`,
   `unsafe or unsupported path` or `Failed to fetch PRs: network error` emitted
   inside such a test does not establish a real environment failure. Conversely,
   a passing negative test does not establish live access either.
4. Separate a product regression, a broken test, an unavailable check and an
   actual external-service failure using that evidence. If a mock returns the
   wrong type, fix its contract; do not request production secrets for it. A
   check that ran and failed is not an unavailable check.
5. Reproduce the failed test, fix the cause, rerun it, then run the affected
   required suite after the final edit. Do not replace behavior checks with
   AST parsing or a hand-written script that only compiles the files.

## Review claims must survive verification

Before accepting an AI-review/Sonar suggestion, read the current code, callers,
consumers and regression tests. State the concrete failure that the proposed
change fixes. A reviewer label or confidence score is not proof.

Removing an output field requires checking workflows/API consumers; a field
such as `read_coverage` can be needed even on failure. Reverting a condition can
restore an older bug. Keep the test that protects that behavior. If a suggestion
breaks it, investigate the contradiction instead of weakening the assertion.
Explain a false positive with current evidence; resolve or publish only when
authorized. A passed quality gate does not establish zero issues or severity.

## Access discovery without leaking credentials

Use the environment and tools that will perform the operation, not a different
shell or another agent's assumption. Consult the installed workspace AGENTS.md
integration sections and approved credential names first.

- GitHub: use the managed `gh` wrapper and `gh auth status` (never `--show-token`),
  then a read-only request for the exact repository. The wrapper already handles
  `GH_TOKEN`, `GITHUB_TOKEN` and `GITHUB_PERSONAL_ACCESS_TOKEN`, including the
  managed Hermes environment. Do not infer failure from `echo $GITHUB_TOKEN`.
- Sonar/Azure: use `integration_token` from the managed DevOps instructions
  inside the request process. It checks the process environment, then the active
  Hermes home's `.env` with interpolation disabled. Build the authorization
  header in memory and send it only to the configured service with TLS and a
  finite timeout. Never print the token or the entire environment.
- A local repository `.env`, the VPS managed `.env`, encrypted Ansible Vault and
  GitHub Actions secrets are separate stores. Do not assume they are synchronized
  or copy secrets between them. Only inspect a local `.env` programmatically
  when it is an explicitly approved source; do not source it as shell code.
- Missing/unreadable credentials, DNS/connection/timeout errors, HTTP 401, 403
  and rate limits are different observations. A 403 alone does not prove which
  permission is missing; inspect the service's safe diagnostic and target.
  Never disable TLS, bypass a sandbox, expose secrets or broaden token scopes.
- If Sonar access is unavailable, the managed instructions describe checking
  GitHub annotations for the requested revision. This can supply issue details;
  it does not establish successful direct Sonar access.

Only request an owner action after checking the approved sources and available
read-only alternatives. Report the failed operation/status without secret-bearing
headers, URLs or raw exception dumps. Ask for the smallest missing access or
managed configuration change, never a token pasted into chat.

## Close the task honestly

For each acceptance criterion retain its command/artifact, revision, result and
remaining action. Report checked, failed, pending and unavailable separately.
Do not call work ready while a required check is failing. Continue authorized
repository fixes; do not invent a separate approval requirement for ordinary
workflow/config edits. Stop for a concrete access boundary or exhausted runtime
budget, with the unfinished work and evidence needed to continue. Never claim a
production deployment or message delivery from local test success.

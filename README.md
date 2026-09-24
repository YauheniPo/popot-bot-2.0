# popot-bot-2.0

[![Telegram user info bot CI](https://github.com/YauheniPo/popot-bot-2.0/actions/workflows/telegram-user-info-bot-ci.yml/badge.svg)](https://github.com/YauheniPo/popot-bot-2.0/actions/workflows/telegram-user-info-bot-ci.yml)
[![Mirror to Azure DevOps](https://github.com/YauheniPo/popot-bot-2.0/actions/workflows/mirror-to-ado.yml/badge.svg)](https://github.com/YauheniPo/popot-bot-2.0/actions/workflows/mirror-to-ado.yml)
[![Build Status](https://dev.azure.com/YauheniPo/popot-bot-2.0/_apis/build/status%2Fpopot-bot-2.0%20AI%20reviewer?branchName=main)](https://dev.azure.com/YauheniPo/popot-bot-2.0/_build/latest?definitionId=12&branchName=main)
[![Build Status](https://dev.azure.com/YauheniPo/popot-bot-2.0/_apis/build/status%2Fpopot-bot-2.0%20Deploy?branchName=main)](https://dev.azure.com/YauheniPo/popot-bot-2.0/_build/latest?definitionId=13&branchName=main)
[![Secured by GitGuardian](https://img.shields.io/badge/Secured%20by-GitGuardian-0b1e39?logo=gitguardian&logoColor=white)](https://www.gitguardian.com/)

[![SonarQube Cloud](https://sonarcloud.io/images/project_badges/sonarcloud-light.svg)](https://sonarcloud.io/summary/new_code?id=YauheniPo_popot-bot-2.0)
[![Quality gate status](https://sonarcloud.io/api/project_badges/measure?project=YauheniPo_popot-bot-2.0&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=YauheniPo_popot-bot-2.0)
[![Bugs](https://sonarcloud.io/api/project_badges/measure?project=YauheniPo_popot-bot-2.0&metric=bugs)](https://sonarcloud.io/summary/new_code?id=YauheniPo_popot-bot-2.0)
[![Vulnerabilities](https://sonarcloud.io/api/project_badges/measure?project=YauheniPo_popot-bot-2.0&metric=vulnerabilities)](https://sonarcloud.io/summary/new_code?id=YauheniPo_popot-bot-2.0)
[![Code Smells](https://sonarcloud.io/api/project_badges/measure?project=YauheniPo_popot-bot-2.0&metric=code_smells)](https://sonarcloud.io/summary/new_code?id=YauheniPo_popot-bot-2.0)
[![Duplicated Lines (%)](https://sonarcloud.io/api/project_badges/measure?project=YauheniPo_popot-bot-2.0&metric=duplicated_lines_density)](https://sonarcloud.io/summary/new_code?id=YauheniPo_popot-bot-2.0)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=YauheniPo_popot-bot-2.0&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=YauheniPo_popot-bot-2.0)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=YauheniPo_popot-bot-2.0&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=YauheniPo_popot-bot-2.0)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=YauheniPo_popot-bot-2.0&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=YauheniPo_popot-bot-2.0)
[![Lines of Code](https://sonarcloud.io/api/project_badges/measure?project=YauheniPo_popot-bot-2.0&metric=ncloc)](https://sonarcloud.io/summary/new_code?id=YauheniPo_popot-bot-2.0)

> Self-hosted Telegram automation and a personal AI operator for your own VPS.

This repository contains two independent projects for people who want useful
automation without handing their server, credentials, or Telegram workflow to a
third party. Start with the component that solves your problem; they do not
depend on one another.

## What can I do with it?

| Project | Use it when you want to… | Start here |
| --- | --- | --- |
| **Hermes Agent on VPS** | run an AI assistant 24/7 from Telegram, work with code and GitHub, research the web, automate browser tasks, and monitor a dedicated VPS | [Hermes overview and quick start](hermes/README.md) |
| **Telegram User Info Bot** | inspect the data Telegram deliberately exposes in a private chat and let a user voluntarily share a phone number or location | [Bot setup guide](telegram-user-info-bot/README.md) |

## Why this repository?

Most agent demos stop at a local chat window. The Hermes setup here focuses on
the less glamorous parts needed to keep an agent useful after the demo:

- repeatable Debian/Ubuntu VPS provisioning with Ansible;
- encrypted configuration delivery through Ansible Vault;
- Telegram gateway, web dashboard, browser automation, and developer CLI tools;
- local health checks, audit metadata, Grafana/Prometheus, and scheduled
  snapshots/backups;
- optional Tailscale-based private access and a documented restore path.

The Telegram bot is intentionally different: it is a small, consent-first
reference project for the Telegram Bot API. It only produces reports in private
chats and sends a phone number or location to external services only after the
user explicitly shares it.

## Choose a path

### 1. Run a personal AI operator on a VPS

Use this if you have a fresh Debian/Ubuntu VPS and want Hermes to stay online
after reboot.

```bash
git clone https://github.com/YauheniPo/popot-bot-2.0.git
cd popot-bot-2.0

# Read these before placing any credentials on the server.
open hermes/README.md
open hermes/SECRETS-CHECKLIST.md
```

For a reproducible deployment, follow the Ansible flow in this order:

1. Review the neutral, git-tracked
   [`inventory.ini`](hermes/ansible/inventory.ini) (connection details load
   from the encrypted Vault, not this file).
2. Create and encrypt the local Vault as described in
   [`VAULT.md`](hermes/ansible/group_vars/all/VAULT.md).
3. Run the command in [the Hermes quick-start guide](hermes/README.md).

Prefer a local dry run first? See the [Docker verification guide](hermes/docker/README.md).

### 2. Run the Telegram User Info Bot locally

Use this if you need a transparent Bot API report tool rather than an AI agent.

```bash
cd telegram-user-info-bot
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Add only your BotFather token to .env, then:
python bot.py
```

See the [full setup guide](telegram-user-info-bot/README.md), including the
privacy model, available commands, and tests.

## Hermes at a glance

```text
Telegram / dashboard
        │
        ▼
Hermes gateway ──► model providers, web tools, browser, GitHub CLI
        │
        ├──► workspace, sessions, skills, backups
        └──► health checks, audit metadata, Prometheus and Grafana

Ansible + Vault ──► repeatable VPS configuration and secret delivery
```

The full installation is designed around a dedicated host. It can give Hermes
terminal access and, if explicitly enabled, passwordless `sudo`. Treat that as
root-equivalent authority: use a separate VPS, restrict Telegram access, and
do not add credentials the agent should never be able to use.

## Security and privacy

- Never commit `.env`, `vault.yml`, OAuth files, private keys, or backup
  archives. The repository includes ignored local-file paths and safe
  `*.example` templates only. `inventory.ini` itself is a neutral, tracked
  alias with no secrets — just don't commit a local edit that adds a real
  `ansible_ssh_private_key_file` path.
- Keep VPS credentials and provider keys in encrypted Ansible Vault. The
  exact workflow is in [`hermes/ansible/group_vars/all/VAULT.md`](hermes/ansible/group_vars/all/VAULT.md).
- The User Info Bot does not obtain a phone number, location, private message
  history, IP address, or contact list automatically. See its
  [privacy details](telegram-user-info-bot/README.md#voluntarily-sharing-data).
- Local backups help recover from a bad update; they do not protect against
  losing the VPS. The planned encrypted offsite-backup work is tracked in
  [`hermes/VPS-BACKLOG.md`](hermes/VPS-BACKLOG.md).

## AI code reviews

The repository has two review engines: a direct API reviewer and a Claude Code
reviewer. Configure their providers and models independently under GitHub
repository **Settings → Secrets and variables → Actions → Variables**.

| Setting | Direct API, including manual/Azure reviews | Claude Code |
| --- | --- | --- |
| Provider | `DIRECT_REVIEW_PROVIDER` | `CLAUDE_REVIEW_PROVIDER` |
| Primary model | `DIRECT_REVIEW_MODEL` | `CLAUDE_REVIEW_MODEL` |
| Fallback model on the same provider | `DIRECT_REVIEW_FALLBACK_MODEL` | `CLAUDE_REVIEW_FALLBACK_MODEL` |
| Custom API base URL | Selected by the provider adapter | `CLAUDE_REVIEW_BASE_URL` (optional) |

Use a model ID accepted by the selected provider. The provider selector supports
these values and chooses the corresponding GitHub Actions **Secret**:

| Provider value | API key secret |
| --- | --- |
| `nvidia` | `NVIDIA_API_KEY` |
| `ollama-cloud` | `OLLAMA_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY` |
| `nous` (Nous Portal) | `NOUS_API_KEY` |

The default provider is `nvidia`. If `DIRECT_REVIEW_PROVIDER` is omitted when
running the preflight script directly, `NVIDIA_API_KEY` is therefore required;
set the provider explicitly when only another provider is configured.

Add the keys for the providers you intend to use. VPS Ansible Vault credentials
are separate and do not reach GitHub runners. Azure needs `GITHUB_ACTIONS_TOKEN`
to launch and collect the GitHub review; the inference key belongs on GitHub.
Changing a model does not require a new variable name or a code edit.

For Nous Portal, generate an inference key in [Portal API keys](https://portal.nousresearch.com/api-keys)
and save it as the GitHub Actions secret `NOUS_API_KEY`. Portal documents
[API-key bearer authentication](https://portal.nousresearch.com/api/openapi).
The CI integration uses that key; it does not import or refresh the VPS Hermes
OAuth session.

To select Portal for either or both engines, set the corresponding Variables
below, replacing the model placeholders with IDs available to your Portal key:

```dotenv
DIRECT_REVIEW_PROVIDER=nous
DIRECT_REVIEW_MODEL=<portal-model-id>
CLAUDE_REVIEW_PROVIDER=nous
CLAUDE_REVIEW_MODEL=<portal-tool-capable-model-id>
```

The existing `DIRECT_REVIEW_FALLBACK_MODEL` and `CLAUDE_REVIEW_FALLBACK_MODEL`
also accept Portal model IDs. Manual GitHub and Azure runs select `provider=nous`
and supply `model` in their run parameters. For the standard Portal connection,
direct review uses Chat Completions. Do not assume a Portal catalog entry also
supports Anthropic Messages: live checks returned HTTP 404 on the standard
Portal Messages route. Use a verified Anthropic-compatible route for Claude and
Observable. Claude runs a short smoke test before starting:
it checks tool calling and the exact review JSON contract in separate requests.
The Claude smoke test uses up to two attempts with a 90-second timeout per check, so an
unavailable or incompatible model is reported before the full Claude run.
Model IDs are passed verbatim, and model access is checked using the CI key.

`PR_REVIEWER=0` (also the unset default) runs all three reviewers:

```text
Direct API
  ├── Claude Code Plugin
  └── Observable Messages Review
```

The two agent jobs start **in parallel after Direct API finishes**, including
when Direct API fails. `1` runs only Direct API; `2` runs both agent reviewers
without Direct API. Owner-only, same-repository and non-draft safeguards apply
to all three. A newer revision cancels the previous run.

Both agent reviewers use `CLAUDE_REVIEW_*` provider and endpoint settings, not
`DIRECT_REVIEW_*`. Observable overrides the model names using
`OBSERVABLE_REVIEW_MODEL` and `OBSERVABLE_REVIEW_FALLBACK_MODEL`; the fallback
provider remains `CLAUDE_REVIEW_FALLBACK_PROVIDER`. Their publication identities are
fixed in code: `ClaudeCodePlugin` and `ObservableMessagesReview`. No additional
identity variables or credentials are required. Each publishes its own PR
summary and inline findings. The observable reviewer never resolves or replies
to other reviewers' threads, avoiding races between the parallel jobs.

Direct API retries HTTP 429 on a dedicated ladder of 1, 2, 5 and 10 minutes
that does not consume its general four-request budget for transport, JSON and
schema retries; a longer provider `Retry-After` extends a step up to 15 minutes,
and the review deadline still bounds every wait. Other retryable errors keep
exponential backoff (1, 2, 4 seconds) with provider hints capped at 90 seconds.

Observable review retries HTTP 429 with exponential backoff (30s, then 60s)
and honors a longer `Retry-After` or rate-limit reset hint. Waiting is capped at
120 seconds per diff chunk across both routes; a longer hint skips that route
instead of retrying before the reset. During backoff, CI logs a heartbeat every
15 seconds explicitly saying that no provider request is in flight. A confirmed
OpenRouter free-model daily limit skips another free model on the same provider;
an upstream/model limit still permits the configured fallback. No unconfigured
model or paid OpenRouter route is selected. When no route remains usable due to limits,
remaining chunks are skipped and the job fails rather than reporting a clean
review. The expandable execution history separates request duration from retry
waiting and shows sanitized limit diagnostics, while the summary shows validated,
failed and skipped chunk counts. Partial results remain in the JSON report but
are not published as a complete review. A bare 429 does not prove daily quota
exhaustion; see [OpenRouter rate-limit guidance](https://openrouter.ai/docs/api_reference/limits).

All three publishers suppress repeats of settled reviewer findings, including
Observable threads. That read-only recognition does not let Claude or Direct API
automatically resolve Observable threads. Direct API includes its diff chunks in
the model request. Both agent reviewers require successful diff-read evidence:
Claude Code checks a matching Read call and non-error, numbered tool result in
its SDK execution file before accepting any primary/retry/fallback result.
Calls without results, empty pages and reads of unrelated files do not count;
failure triggers the existing retry/fallback path with `diff_not_read`.
This is an input-access check, not a guarantee of exhaustive or accurate review.

Manual GitHub review and the Azure launcher use the same
direct reviewer. Their `provider` and `model` come from run inputs, including
the defaults displayed in the launch form. Set both there; changing the PR's
`DIRECT_REVIEW_MODEL` repository variable does not change that form's default.
The direct reviewer uses `DIRECT_REVIEW_MODEL` for every supported provider.
The model validated by preflight is passed to the review step automatically.

The Azure summary links to the GitHub run, published review and available
PR/findings pages. Review-report artifacts remain uploaded for collection and
diagnostics; the summary does not include a separate artifact-page link.

For a pull request opened by someone else, use the `ai-review-approved` label
after you have inspected the change. Only the repository owner adding that label
starts the owner-approved direct review. The workflow removes the label before
reviewing, so every new revision needs a deliberate new approval. It runs trusted
review tooling from `main` and treats the pull-request checkout as data only;
the contributor's scripts are never executed with review credentials. A new
commit cancels an in-progress review of the old revision. The label is created
in the repository and can be applied from the pull request's Labels menu.

Configure each fallback model together with its matching `*_FALLBACK_PROVIDER`;
the provider may differ from the primary. Workflows supply the documented defaults
when repository variables are unset. `CLAUDE_REVIEW_BASE_URL` overrides only the
primary provider's Claude endpoint while keeping the
selected provider's credentials; it must accept Claude Code's API and tool calls.
For NVIDIA, the hosted Chat Completions endpoint is used by the direct reviewer.
Claude with NVIDIA requires an Anthropic-compatible gateway set explicitly in
`CLAUDE_REVIEW_BASE_URL`. The adapter accepts the API root, a base ending in `/v1`,
or the full `/v1/messages` URL and normalizes it once for both preflight and all
Claude stages. For example, `https://openrouter.ai/api/v1` becomes the SDK base
`https://openrouter.ai/api`, with requests sent to `/api/v1/messages`.
Without a gateway URL for NVIDIA, Claude
preflight stops before making a model request. Ollama Cloud, OpenRouter, and Nous
have candidate Messages routes in the adapter; availability must be established
by live tool and JSON probes, not inferred from the model catalog. A fallback on
a different provider uses that provider's route, never the primary URL override.

### Review availability and rate limits

All three methods share the account-wide free quota when using OpenRouter
`:free` models. Switching free models does not bypass an exhausted daily quota.
Preflight honors `Retry-After` within a bounded wait budget and skips same-provider
free fallback after confirmed daily exhaustion. A separately configured provider
can still be tried. OpenRouter routes must use existing free model IDs; adding
`:free` to a paid model ID does not create a free route.

Default fallback routes use `ollama-cloud`: Direct API → `deepseek-v4.1-flash`,
Claude Code → `glm-5.3-flash`, Observable → `kimi-k2.7-code`. Each passed a live
synthetic diff review on 2026-09-20 (JSON plus file/tool reads for the agents).
This validates compatibility, not future uptime or review accuracy on all code.
Set `OLLAMA_API_KEY` in GitHub Actions secrets. Repository variables override
these defaults; update stale fallback model/provider pairs together. Leave
`CLAUDE_REVIEW_BASE_URL` unset when using the providers' standard endpoints.

The `review-results` job checks that **every selected method** actually returned
and published a validated result. A green diagnostic/publication step is not
proof of a completed review. Missing results fail this aggregate check even when
the individual corroborating jobs use `continue-on-error` to publish diagnostics.
Direct preflight **and review/publication failures** and Claude preflight reasons are reported on the PR;
Observable reports completed, failed and skipped chunks. Retry only after the
reported cooldown/reset or after fixing the model/provider configuration.

Preflight checks run before Direct API and Claude Code review. The direct reviewer requires valid review
JSON; Claude Code also requires tool calling through an Anthropic-compatible
endpoint. Provider selection alone does not establish model or API compatibility;
the supported probes are implemented in
[`ai_review_preflight.py`](.github/scripts/ai_review_preflight.py).
Both primary and configured fallback must pass the appropriate probe before
being marked ready. A distinct fallback adds a probe with at most four HTTP
attempts; when it equals the primary, that result is reused without another
request. A failed fallback probe emits a warning and disables the fallback for
that run while keeping the validated primary. If the primary fails its probe,
preflight still checks the configured distinct fallback: direct review starts
with the validated fallback, and Claude skips primary runs and uses its fallback
stage. If neither model passes, preflight fails without starting review. A
fallback equal to the failed primary does not grant another retry budget.
Each probe attempt logs its model, attempt number, and timeout. Preflight calls
may incur provider charges, even though they are excluded from the published
review request totals.
Direct JSON preflight requests `stream=true` and uses the same bounded response
reader as the full review. Providers returning an ordinary JSON response remain
supported. A successful small probe checks transport compatibility, not whether
a full diff will finish within the provider's limits.

Both direct preflight and review emit one `[direct-review] request_end` JSON
record per request, including failures before the first heartbeat. It contains
elapsed/idle time, HTTP status when available, response format, received byte
and SSE event counts, content/reasoning character counts, and the observed
`finish_reason`, `done_seen` and `eof_seen`. Prompts, model text, reasoning text,
headers, credentials and raw provider errors are not logged; unknown finish
reasons are recorded as `other`. The preceding attempt line identifies the model
and, for full reviews, the route and diff chunk. `received` means transport
completed; JSON and review validation still have to pass.

For `stream_incomplete`, `eof_seen=true` with `done_seen=false` means the stream
closed without `[DONE]`; `done_seen=true` with `finish_reason=none` means the
terminal marker arrived without a completion reason. Normally even
`finish_reason=stop` without `[DONE]` is rejected. Nous is the verified exception:
its SSE route closes after `stop` without `[DONE]` (observed for both configured
models in [run 35851663557](https://github.com/YauheniPo/popot-bot-2.0/actions/runs/35851663557)).
For Nous only, preflight and review accept a clean EOF after `stop`, then apply
the existing JSON and review validation. Missing `stop`, read errors, timeouts,
provider errors and invalid JSON still fail. Diagnostics retain `done_seen=false`
and `eof_seen=true` for this completion path.
`finish_reason=length` is an `output_limit`, including
when the stream ends without `[DONE]`. These records distinguish observed wire
events, but cannot prove why a remote connection ended. Incomplete streams are
not retried on the same model; the configured fallback is tried instead.
Timeout retries retain the existing bounded attempt and wait budgets.

The direct adapter selects the fallback response format automatically, including
an ordinary-JSON retry when a schema request is explicitly rejected as
unsupported. No fallback-mode variable is required. The publisher validates
review JSON and exact diff anchors locally before posting findings.

Direct review allows at most four API requests per model for each chunk or
thread-triage operation. Transport retries, invalid-JSON regeneration, and API
compatibility adjustments share that limit. A different fallback has its own
four-request limit; the total time budget can stop either model earlier.
Repeated idle/total timeouts stop a model route after two attempts, then use a
configured independent fallback if available. They do not spend four identical
requests on a silent route. No extra provider or model is selected implicitly.

Direct requests use Chat Completions streaming, with a **90-second inactivity
limit**, **300-second absolute limit per request** (also capped by the remaining
review budget), and a **heartbeat every 30 seconds**, including while connecting.
Logs show elapsed/idle time, state, received events, and content/reasoning character
counts, never the reasoning or response text. SSE keepalives do not reset the
inactivity deadline. `provider_processing=unknown` is intentional: runner
liveness does not prove the provider is thinking. A stream must terminate cleanly
before JSON/schema/anchor validation; partial responses are not successful reviews.
Ollama receives the requested reasoning effort via its supported
[`reasoning_effort`](https://docs.ollama.com/api/openai-compatibility) field,
instead of silently reverting to the model's default thinking mode. Nous receives
the nested `reasoning` object supported by its gateway, including `effort=none`
and the existing `effort=low` retry when a model requires reasoning. Both primary
and fallback review requests retain these controls; they are requests to the
provider, not a guarantee that every model will finish within the deadline.
Wire data is capped at 16 MiB, including SSE framing, independently of the token budget.
Non-streaming responses remain supported under the same watchdog. Failures have
explicit reasons such as `inactivity_timeout`, `attempt_timeout`, `connection_error`,
`stream_incomplete`, or `output_limit`. CI's step summary records request history;
the PR failure comment identifies the selected route and links to the failed run.
Claude Code's full-run retry stages are configured separately in the PR workflow.
The direct PR job allows 60 minutes and manual/Azure review 105 minutes, including
preflight and publication; their model-traffic budgets remain separate.

Tune traffic and execution budgets for your provider's quota and the size of the
review. `OLLAMA_REVIEW_RPM` controls direct API request pacing,
`OLLAMA_REVIEW_COOLDOWN_SECONDS` controls the pause between the Direct API
reviewer and Claude Code (useful when the two reviewers share a quota),
and `OLLAMA_REVIEW_BUDGET_SECONDS` bounds direct review model traffic. These
historical variable names also apply when a different provider is selected.
Manual/Azure review uses `MANUAL_REVIEW_MAX_CHUNKS` and
`MANUAL_REVIEW_BUDGET_SECONDS` for its chunk and time budgets.

### Observable reviewer: progress and failure handling

The third reviewer uses a separate streaming Messages API tool loop, not the
Claude Code SDK. It can only Read, Glob and literal-Grep tracked regular files
and a generated diff; it cannot execute shell commands or read symlink targets,
untracked credentials or `.git`. Reads and searches are bounded; the prompt
requires it to disclose any unreviewed scope. The full base-to-head diff is
split into bounded chunks (≤32 000 chars) before review; each chunk is written
to the generated diff in turn and reviewed separately, and their validated
findings are merged into one report capped at five. Chunking keeps every slice
small enough for a free/small model to finish with a valid `end_turn` instead
of exhausting its output budget (`provider_incomplete_result`). Final JSON is
rejected with `diff_not_read` unless a Read call returned actual numbered diff
lines; failed reads and empty pages do not count. A `diff_not_read` rejection
is treated like any other failed attempt and follows the same per-route retry
and fallback path. This proves access to
changes, not complete coverage. Oversized lines are omitted individually
without blocking later pages.

The last model turn is reserved for a JSON-only response, within the existing
turn/deadline budget. Tools are removed for that request and their earlier
calls/results are preserved as text evidence. This avoids relying on
`tool_choice` controls, which [Ollama Cloud does not fully support](https://docs.ollama.com/api/anthropic-compatibility).
Identical tool calls reuse earlier results without reading/searching again;
different offsets and queries still work. Three consecutive rounds containing
only repeated calls trigger finalization early. CI logs `tool_cache_hit` and
`finalization_started reason=turn_budget` or `reason=repeated_tools`.
Finalization does not waive JSON, diff-read or changed-line validation. A model
that still requests tools fails with `turn_limit`; unread scope must still be
disclosed rather than described as a complete review.

It has no separate preflight step of its own:
tool support and final JSON are validated during the actual review attempts,
so an unusable route fails the attempt rather than a standalone check.

CI logs show provider/model, attempt, tool start/completion and periodic
heartbeat lines. Streaming content/reasoning events update the activity counter
without logging source code, reasoning text, credentials or response bodies.
`provider_processing=unknown` deliberately makes no claim about hidden provider
work: check `state`, `events` and `last_activity` for received activity. SSE
keepalive pings alone do not count as model progress.

Optional repository variables for this runner (all retain `CLAUDE_REVIEW_` names):

| Variable | Default | Maximum | Meaning |
| --- | --- | --- | --- |
| `CLAUDE_REVIEW_MAX_TURNS` | 24 | 96 | Total model turns per attempt, including the final JSON-only turn |
| `CLAUDE_REVIEW_ATTEMPT_TIMEOUT_SECONDS` | 600 | 900 | Hard deadline per attempt |
| `CLAUDE_REVIEW_INACTIVITY_TIMEOUT_SECONDS` | 180 | 600 | Deadline without substantive provider/tool events |
| `CLAUDE_REVIEW_HEARTBEAT_SECONDS` | 30 | 60 | CI heartbeat interval |
| `CLAUDE_REVIEW_MAX_TOKENS` | 8192 | 32768 | Output-token budget per model turn |

An independent watchdog terminates a stalled worker. A transient failure,
invalid JSON or invalid diff anchor allows one fresh retry, then up to two
attempts on the distinct configured fallback route. Non-retryable HTTP
400/401/403/404 or unavailable credentials skip directly to the fallback;
an identical fallback does not add attempts. Every attempt starts a new review
conversation. The job's 70-minute cap leaves publication time even with four
maximum-length attempts. Parallel agent jobs share provider quotas; configure
budgets accordingly.

The PR summary and CI step summary contain a provider/model/outcome/time table
and the PR summary links to the run and reviewed revision. Exhausted attempts
fail the job and publish **no validated review result**, never a clean-review
claim. Inline-anchor rejection retains the finding in the summary. Re-running
the same SHA creates a fresh report; retrying publication within one run attempt
does not duplicate already published comments. Before creating inline threads,
the publisher also matches findings against open threads and settled machine
findings across runs and revisions. Repeats and additional evidence for existing
threads are listed in the fresh summary without opening duplicate threads or
modifying existing ones. Cancelled jobs skip publication;
an already in-flight GitHub write may still complete. This runner does not change
Claude Code's existing retry settings.

Current defaults, retry limits, and job timeouts live in the
[PR workflow](.github/workflows/pr-ai-review.yml),
[manual workflow](.github/workflows/manual-ai-review.yml),
[Azure launcher](azure-ci/azure-ai-code-review.yml), and
[review script](.github/scripts/ai_pr_review.py). Use those files and the selected
run inputs as the source of configured values; use the published execution
history to see which model and attempts actually produced a result.

Published reviews include execution evidence. Direct API reviews and manual/Azure
Check Runs show the connection, successful models, request/retry totals, fallback
successes, API time, and a bounded request history. Inline findings
and thread replies identify their successful request and primary/fallback route.
Response IDs and reported model IDs are included when the API returns them;
preflight and GitHub publication calls are excluded from the model request counts.
Retries count additional requests for the same chunk or thread-triage operation;
five chunks completed in five requests report zero retries. Request numbers are
global positions in the execution history.
When preflight selects a backup, successful review requests still count as
fallback successes. The workflows pass the original primary through internal
`DIRECT_REVIEW_PRIMARY_MODEL` metadata; no additional repository variable is needed.
Claude comments report the actual successful CI attempt and its configured model,
including prior execution or result-validation failures. Claude SDK HTTP retries
are not exposed by this workflow and are explicitly marked as unrecorded.
Credentials, prompts, raw response/error bodies, and custom endpoint addresses
are never included in this execution metadata.
The metadata is excluded from future review prompts and duplicate matching.

Historical provider names in job IDs, environment aliases, and inline-comment
markers are retained for compatibility. They do not identify the connection
used by a particular run.

## Coverage quality gate

The project's SonarQube Cloud gate is
`popot-bot-2.0: coverage 100% new / 75% overall`:

- New/changed code: **100%** coverage.
- Overall code (old and new combined): **at least 75%** coverage.
- The other Sonar way conditions on reliability, security, maintainability,
  reviewed security hotspots, and duplication remain enabled.

These thresholds are configured in the project's server-side **Quality Gate**,
not as scanner properties. **Ignore duplication and coverage on small changes**
is disabled for this project, so changes below 20 new lines are checked too.
The organization-wide default gate is unchanged.

The [Sonar workflow](.github/workflows/sonarcloud.yml) uploads both Python coverage
reports and waits up to 300 seconds for the gate result. A failed gate or timeout
fails the scan job. Dependabot PRs still run tests but skip the authenticated scan
because the Sonar token is unavailable to them.

Sonar checks only new-code conditions in pull requests; both new-code and overall
conditions apply on `main`. Thus the 75% overall threshold is not a pre-merge PR
check. On PRs, new code means changes against the target branch; on `main`, it
uses the project's configured new-code period. See
[Sonar's gate semantics](https://docs.sonarsource.com/sonarqube-cloud/standards/managing-quality-gates/introduction-to-quality-gates).

To block merges, configure the Sonar quality-gate check as a required check in
GitHub branch protection/rulesets. A failing workflow alone does not enforce
branch protection; account for the Dependabot scan exception when choosing
required checks.

## Repository map

```text
.
├── azure-ci/                # Azure DevOps pipeline definitions and shared templates
├── hermes/                  # VPS deployment, operations, observability and docs
│   ├── ansible/             # repeatable provision/restore workflow
│   ├── docker/              # local verification environment
│   └── ops/                 # health checks, backup, metrics and dashboards
└── telegram-user-info-bot/  # standalone consent-first Telegram bot
```

## Project status and contributions

This is an actively evolving personal infrastructure repository. Issues and
focused pull requests are welcome: please explain the user problem, keep
credentials out of the diff, and include the smallest relevant verification.
For Hermes improvements, check the [VPS backlog](hermes/VPS-BACKLOG.md) first.

## License

Licensed under the [Apache License 2.0](LICENSE).

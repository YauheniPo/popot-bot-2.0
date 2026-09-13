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
leave `CLAUDE_REVIEW_BASE_URL` unset. Direct review uses Chat Completions; Claude
uses the Portal Messages endpoint and checks tool calling before starting.
Model IDs are passed verbatim, and model access is checked using the CI key.

`PR_REVIEWER` selects both reviewers sequentially (`0`/unset), direct API (`1`),
or Claude Code (`2`). Manual GitHub review and the Azure launcher use the same
direct reviewer. Their `provider` and `model` come from run inputs, including
the defaults displayed in the launch form. Set both there; changing the PR's
`DIRECT_REVIEW_MODEL` repository variable does not change that form's default.
The direct reviewer uses `DIRECT_REVIEW_MODEL` for every supported provider.
The model validated by preflight is passed to the review step automatically.

Set a fallback model to select a backup on the same provider. An unset
`DIRECT_REVIEW_FALLBACK_MODEL` disables switching to a backup model. For Claude,
an unset `CLAUDE_REVIEW_FALLBACK_MODEL` makes the fallback stage use the primary
model. `CLAUDE_REVIEW_BASE_URL` overrides the Claude endpoint while keeping the
selected provider's credentials; it must accept Claude Code's API and tool calls.
For NVIDIA, the hosted Chat Completions endpoint is used by the direct reviewer.
Claude with NVIDIA requires an Anthropic-compatible gateway set explicitly in
`CLAUDE_REVIEW_BASE_URL`. The adapter accepts the API root, a base ending in `/v1`,
or the full `/v1/messages` URL and normalizes it once for both preflight and all
Claude stages. For example, `https://openrouter.ai/api/v1` becomes the SDK base
`https://openrouter.ai/api`, with requests sent to `/api/v1/messages`.
Without a gateway URL for NVIDIA, Claude
preflight stops before making a model request. Ollama Cloud, OpenRouter, and Nous
have explicit Messages routes in the adapter; the tool probe checks the selected
model against that route or your override.

Preflight checks run before review. The direct reviewer requires valid review
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
The direct adapter selects the fallback response format automatically, including
an ordinary-JSON retry when a schema request is explicitly rejected as
unsupported. No fallback-mode variable is required. The publisher validates
review JSON and exact diff anchors locally before posting findings.

Direct review allows at most four API requests per model for each chunk or
thread-triage operation. Transport retries, invalid-JSON regeneration, and API
compatibility adjustments share that limit. A different fallback has its own
four-request limit; the total time budget can stop either model earlier.
Claude Code's full-run retry stages are configured separately in the PR workflow.
The direct PR job allows 60 minutes and manual/Azure review 105 minutes, including
preflight and publication; their model-traffic budgets remain separate.

Tune traffic and execution budgets for your provider's quota and the size of the
review. `OLLAMA_REVIEW_RPM` controls direct API request pacing,
`OLLAMA_REVIEW_COOLDOWN_SECONDS` controls the pause between the two PR reviewers,
and `OLLAMA_REVIEW_BUDGET_SECONDS` bounds direct review model traffic. These
historical variable names also apply when a different provider is selected.
Manual/Azure review uses `MANUAL_REVIEW_MAX_CHUNKS` and
`MANUAL_REVIEW_BUDGET_SECONDS` for its chunk and time budgets.

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

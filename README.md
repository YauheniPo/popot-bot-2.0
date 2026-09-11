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

All CI code reviewers are pinned in code to **Kimi K3 on Ollama Cloud**:
the direct API reviewer, Claude Code (including its role models and retries),
the manual GitHub review, and the Azure DevOps review launcher. The direct
Cloud API model ID is `kimi-k3`; `kimi-k3:cloud` is the local Ollama alias.

Add **`OLLAMA_API_KEY`** under GitHub repository **Settings → Secrets and
variables → Actions → Secrets**. The key in the VPS Ansible Vault is separate
and does not reach GitHub runners. Azure launches the GitHub workflow, so it
continues to need `GITHUB_ACTIONS_TOKEN`; the Ollama key belongs on GitHub.

The API reviewer uses `https://ollama.com/v1/chat/completions`; Claude Code
uses `ANTHROPIC_BASE_URL=https://ollama.com` with the same key. Live preflight
checks JSON responses and Anthropic tool calling respectively. Ollama Cloud
does not support JSON Schema enforcement, so prompts carry the schema and
the existing publisher validates the response and exact diff anchors locally.
No alternative model is selected if Kimi is unavailable.

`PR_REVIEWER` still selects both reviewers (`0`/unset), direct API (`1`), or
Claude Code (`2`). Model repository variables no longer override the pin.
The default review budget is sized for the unlimited Ollama account: up to
100 diff chunks, 32,768 output tokens per completion, 60 requests per minute,
and a 40-minute direct-review budget. Optional traffic controls are
`OLLAMA_REVIEW_RPM`, `OLLAMA_REVIEW_COOLDOWN_SECONDS`, and
`OLLAMA_REVIEW_BUDGET_SECONDS`.
Manual review retains `MANUAL_REVIEW_MAX_CHUNKS` and
`MANUAL_REVIEW_BUDGET_SECONDS`.

The direct reviewer is `.github/scripts/ollama_pr_review.py`, and its live
model preflight is `.github/scripts/ollama_review.py`. The obsolete OpenRouter
preflight and its tests have been removed. The historical `openrouter-api-review`
job ID and inline-comment markers remain for existing check and review-thread
compatibility; current workflows use Ollama for inference.

See [Ollama Cloud](https://docs.ollama.com/cloud),
[structured output limitations](https://docs.ollama.com/capabilities/structured-outputs),
and [Anthropic compatibility](https://docs.ollama.com/api/anthropic-compatibility).

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

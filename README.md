# popot-bot-2.0

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

1. Create a local ignored inventory from
   [`inventory.example.ini`](hermes/ansible/inventory.example.ini).
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

- Never commit `.env`, `inventory.ini`, `vault.yml`, OAuth files, private keys,
  or backup archives. The repository includes ignored local-file paths and
  safe `*.example` templates only.
- Keep VPS credentials and provider keys in encrypted Ansible Vault. The
  exact workflow is in [`hermes/ansible/group_vars/all/VAULT.md`](hermes/ansible/group_vars/all/VAULT.md).
- The User Info Bot does not obtain a phone number, location, private message
  history, IP address, or contact list automatically. See its
  [privacy details](telegram-user-info-bot/README.md#voluntarily-sharing-data).
- Local backups help recover from a bad update; they do not protect against
  losing the VPS. The planned encrypted offsite-backup work is tracked in
  [`hermes/VPS-BACKLOG.md`](hermes/VPS-BACKLOG.md).

## Repository map

```text
.
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

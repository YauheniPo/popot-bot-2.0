# Claude PR reviewer

`.github/workflows/claude-review.yml` requests a review automatically when a
non-draft pull request is opened, updated, or marked ready. It is intentionally
read-only: the reviewer may leave PR comments but cannot modify repository
contents, approve a PR, merge it, or run on pull requests from forks.

The reviewer automatically reads the root [`CLAUDE.md`](../CLAUDE.md) for its
project and review rules. This file documents GitHub setup for maintainers; it
is not itself an automatic Claude Code instruction.

## One-time GitHub setup

1. In OpenRouter create a separate API key named `github-pr-review`; never
   reuse the key from Hermes or a local machine.
2. Set this key's spending limit to **$1** with a **monthly** reset. The $5
   account balance remains available, but GitHub Actions cannot spend more than
   the key cap. Raise it deliberately only after reviewing usage.
3. In the GitHub repository, open **Settings → Secrets and variables → Actions**.
4. Add repository secret `OPENROUTER_API_KEY` with that key.
5. Open or update a non-draft pull request from a branch in this repository.
   The **Claude PR review** workflow should add findings as PR comments.

The workflow routes Claude Code through OpenRouter's Anthropic-compatible
endpoint. This avoids an Anthropic API key, but it is not a free reviewer:
Claude Code is officially guaranteed only with Anthropic models. Give this key
a small per-key OpenRouter credit limit and inspect costs in OpenRouter before
using the workflow for every pull request.

The workflow explicitly uses `anthropic/claude-3-haiku`, the absolute cheapest
currently available Claude option ($0.25 / 1M input tokens and $1.25 / 1M
output tokens). It is limited to six turns and runs when a PR is opened,
reopened, marked ready, or updated with a new commit. If a newer commit arrives
while review is running, GitHub cancels the older run. For an unchanged PR,
use **Actions → Claude PR review → Re-run jobs** only when you explicitly want
to spend credits on another review.

This is a cost-first choice. If reviews miss important issues, replace all
four model variables and `--model` with `anthropic/claude-3.5-haiku` for a
stronger reviewer ($0.80 / 1M input, $4 / 1M output) while keeping the same
per-key limit.

Do not use `pull_request_target` for this reviewer and do not grant
`contents: write`: both would unnecessarily expose credentials or allow changes
from CI. Pull requests from forks are deliberately skipped; review them
manually or after creating a trusted branch.

## Review focus

- bot behavior remains private-chat-only and consent boundaries stay explicit;
- precise location is disclosed before it reaches Nominatim/OpenStreetMap and
  is not written to logs or persistent storage;
- Telegram API failures do not leak user data, bot tokens, or raw internals;
- Nominatim requests keep their timeout, identification and rate limiting;
- tests cover changed behavior and are not weakened solely to make CI pass.

The reviewer is advisory: merge rules and the existing test workflow remain
the source of enforcement.

# OpenRouter PR reviewer

`.github/workflows/claude-review.yml` performs one bounded OpenRouter request
when a non-draft pull request is opened, updated, or marked ready. It then
creates (or updates) one top-level PR comment. It does not run an autonomous
coding agent, modify repository files, approve or merge pull requests.

## One-time GitHub setup

1. In OpenRouter create a separate API key named `github-pr-review`; never
   reuse the key from Hermes or a local machine.
2. Set the key's monthly spending limit to **$1** (or another deliberate cap).
3. In the repository, open **Settings → Secrets and variables → Actions**.
4. Add repository secret `OPENROUTER_API_KEY` with that key.
5. Open or update a non-draft pull request from a branch in this repository.
   The **OpenRouter PR review** workflow updates its single review comment.

The workflow uses `google/gemini-2.5-flash-lite` directly through OpenRouter,
not Claude Code's multi-turn tool loop. A review is limited to the first 60,000
characters of the textual diff, 900 generated tokens, 350 words and three
findings, so its token use is predictable and its PR comment is not cut off. A
truncated diff is clearly marked in the review. Check actual
charges in OpenRouter; the earlier Claude Code Action `total_cost_usd` field is
an internal estimate and is not an OpenRouter invoice.

The reviewer deliberately excludes its own workflow, script and setup document
from the AI input. That prevents a noisy self-review loop; changes to those
three files require normal human code review.

Pull requests from forks are deliberately skipped. Do not change the event to
`pull_request_target` and do not grant `contents: write`: doing either would
unnecessarily expose credentials or broaden CI authority.

## Review focus

- bot behavior remains private-chat-only and consent boundaries stay explicit;
- precise location is disclosed before it reaches Nominatim/OpenStreetMap and
  is not written to logs or persistent storage;
- Telegram API failures do not leak user data, bot tokens, or raw internals;
- Nominatim requests keep their timeout, identification and rate limiting;
- tests cover changed behavior and are not weakened solely to make CI pass.

The reviewer is advisory: merge rules and the existing test workflow remain
the source of enforcement.

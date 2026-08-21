#!/usr/bin/env python3
"""Run one bounded OpenRouter review and maintain its PR summary comment."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request


COMMENT_MARKER = "<!-- openrouter-pr-review -->"
MAX_DIFF_CHARACTERS = 60_000
MAX_OUTPUT_TOKENS = 900
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GITHUB_API_URL = "https://api.github.com"
REVIEW_RULES_PATH = Path(".github/REVIEWER.md")
EXCLUDED_REVIEW_PATHS = (
    ".github/workflows/claude-review.yml",
    ".github/scripts/openrouter_pr_review.py",
    str(REVIEW_RULES_PATH),
)


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is missing")
    return value


def request_json(url: str, method: str, headers: dict[str, str], body: object | None = None) -> object:
    encoded_body = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=encoded_body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {error.code}: {details[:500]}") from error


def read_diff(base_sha: str, head_sha: str) -> tuple[str, bool]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--unified=3",
            base_sha,
            head_sha,
            "--",
            ".",
            *(f":(exclude){path}" for path in EXCLUDED_REVIEW_PATHS),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    diff = result.stdout
    if len(diff) <= MAX_DIFF_CHARACTERS:
        return diff, False
    return diff[:MAX_DIFF_CHARACTERS], True


def read_review_rules() -> str:
    try:
        rules = REVIEW_RULES_PATH.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(f"Could not read reviewer rules from {REVIEW_RULES_PATH}") from error
    if not rules:
        raise RuntimeError(f"Reviewer rules file {REVIEW_RULES_PATH} is empty")
    return rules


def review_diff(api_key: str, model: str, diff: str, truncated: bool) -> str:
    truncation_note = (
        "The diff was truncated to the first 60,000 characters; explicitly say so in the summary."
        if truncated
        else "The complete diff is included."
    )
    user_prompt = f"""Review this pull-request diff. {truncation_note}

```diff
{diff or '(No textual diff was produced.)'}
```"""
    response = request_json(
        OPENROUTER_URL,
        "POST",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com",
            "X-Title": "popot-bot-2.0 PR review",
        },
        {
            "model": model,
            "temperature": 0,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [
                {"role": "system", "content": read_review_rules()},
                {"role": "user", "content": user_prompt},
            ],
        },
    )
    try:
        content = response["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise RuntimeError("OpenRouter response did not contain a review message") from error
    if not content:
        raise RuntimeError("OpenRouter returned an empty review message")
    return content


def publish_comment(
    repository: str, pr_number: str, token: str, model: str, review: str
) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    comments_url = f"{GITHUB_API_URL}/repos/{repository}/issues/{pr_number}/comments?per_page=100"
    comments = request_json(comments_url, "GET", headers)
    body = f"{COMMENT_MARKER}\n> Provider: OpenRouter · Model: `{model}`\n\n{review}"
    existing_comment = next(
        (comment for comment in comments if COMMENT_MARKER in comment.get("body", "")), None
    )
    if existing_comment:
        request_json(
            f"{GITHUB_API_URL}/repos/{repository}/issues/comments/{existing_comment['id']}",
            "PATCH",
            headers,
            {"body": body},
        )
        print("Updated the OpenRouter PR review comment.")
    else:
        request_json(comments_url.split("?")[0], "POST", headers, {"body": body})
        print("Created the OpenRouter PR review comment.")


def main() -> None:
    api_key = required_env("OPENROUTER_API_KEY")
    github_token = required_env("GITHUB_TOKEN")
    model = required_env("OPENROUTER_REVIEW_MODEL").strip()
    if not model:
        raise RuntimeError("OPENROUTER_REVIEW_MODEL must not be blank")
    repository = required_env("GITHUB_REPOSITORY")
    pr_number = required_env("PR_NUMBER")
    diff, truncated = read_diff(required_env("BASE_SHA"), required_env("HEAD_SHA"))
    if not diff:
        publish_comment(
            repository,
            pr_number,
            github_token,
            model,
            "## OpenRouter API review\nSummary: No application-code changes to review.\nFindings: No actionable findings.",
        )
        return
    review = review_diff(api_key, model, diff, truncated)
    publish_comment(repository, pr_number, github_token, model, review)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"OpenRouter PR review failed: {error}", file=sys.stderr)
        sys.exit(1)

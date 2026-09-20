#!/usr/bin/env python3
"""Load unresolved GitHub review threads and safely reply to them."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, cast
import urllib.error
import urllib.request

from review_execution import claude_execution_report, strip_execution_metadata, technical_metadata


GITHUB_API_URL = "https://api.github.com"
CLAUDE_REVIEWER_LABEL = "ClaudeCodePlugin"
DIRECT_REVIEWER_INLINE_PREFIX = "<!-- direct-openrouter-inline:"
AUTOMATED_REVIEW_AUTHOR = "github-actions[bot]"
# Both reviewers stamp their initial inline comment with their own marker and
# the head SHA they reviewed. The SHA is captured but deliberately not pinned to
# the current revision: a thread opened on an earlier push is exactly the thread
# a later push can close.
MACHINE_INLINE_MARKER = re.compile(
    r"<!--\s*(?:direct-openrouter-inline|claude-inline):([0-9a-f]{40}):",
    re.IGNORECASE,
)
MAX_THREADS = 200
MAX_RESOLVED_THREADS = 200
MAX_CONTEXT_CHARACTERS = 24_000
MAX_RENDERED_COMMENTS_PER_THREAD = 3
MAX_RENDERED_COMMENT_CHARACTERS = 1_000
MAX_REPLY_CHARACTERS = 4_000
MAX_INLINE_COMMENT_CHARACTERS = 4_000
MAX_CLAUDE_EXECUTION_FILE_BYTES = 64 * 1024 * 1024
MAX_CLAUDE_REVIEW_RESULT_BYTES = 1024 * 1024
MAX_CLAUDE_SUMMARY_CHARACTERS = 4_000
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
SEMANTIC_DUPLICATE_THRESHOLD = 0.42
ANCHOR_DUPLICATE_THRESHOLD = 0.24
WORD_PATTERN = re.compile(r"[a-zа-яё0-9_]{4,}", re.IGNORECASE)


def _validate_cli_path(path: Path, label: str) -> Path:
    """Accept only absolute, canonicalizable paths supplied by the CLI."""
    raw_path = str(path)
    if not path.is_absolute() or "\x00" in raw_path or ".." in path.parts:
        raise RuntimeError(f"{label} must be an absolute path without traversal")
    try:
        return path.resolve(strict=False)
    except OSError as error:
        raise RuntimeError(f"{label} is unavailable") from error
STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "before",
        "comment",
        "could",
        "finding",
        "impact",
        "into",
        "issue",
        "proposed",
        "review",
        "should",
        "that",
        "their",
        "there",
        "these",
        "this",
        "through",
        "with",
        "would",
    }
)


@dataclass(frozen=True)
class ReviewComment:
    node_id: str
    database_id: int | None
    author: str
    body: str


@dataclass(frozen=True)
class ReviewThread:
    node_id: str
    path: str
    side: str
    line: int | None
    original_line: int | None
    outdated: bool
    viewer_can_reply: bool
    comments: tuple[ReviewComment, ...]
    resolved: bool = False

    @property
    def reply_to_comment_id(self) -> int | None:
        return self.comments[0].database_id if self.comments else None

    @property
    def searchable_text(self) -> str:
        return " ".join(strip_execution_metadata(comment.body) for comment in self.comments)


@dataclass(frozen=True)
class ThreadMatch:
    thread: ReviewThread
    relationship: str
    similarity: float


@dataclass(frozen=True)
class ReviewFinding:
    severity: str
    path: str
    side: str
    line: int
    title: str
    impact: str
    fix: str


@dataclass(frozen=True)
class ThreadVerdict:
    thread_id: str
    verdict: str
    reason: str


class GitHubRequestError(RuntimeError):
    pass


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is missing")
    return value


def _github_token() -> str:
    # Precedence is deliberate. GitHub Actions exports GITHUB_TOKEN on every
    # run; GH_TOKEN is the documented CLI-level override; OVERRIDE_GITHUB_TOKEN
    # is the explicit escape hatch for the deterministic publish step and for
    # local testing. Keep this order and the names in sync with the workflow env.
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "OVERRIDE_GITHUB_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    raise RuntimeError("A GitHub token is required")


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _request_json(
    url: str,
    method: str,
    token: str,
    body: object | None = None,
) -> Any:
    encoded_body = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded_body,
        headers=_github_headers(token),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise GitHubRequestError(
            f"{method} {url} failed with HTTP {error.code}: {details[:500]}"
        ) from error
    except urllib.error.URLError as error:
        raise GitHubRequestError(f"{method} {url} failed before receiving a response") from error


def _repository_parts(repository: str) -> tuple[str, str]:
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise RuntimeError("GITHUB_REPOSITORY must use owner/name format")
    return parts[0], parts[1]


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_thread(raw: object) -> ReviewThread | None:
    if not isinstance(raw, dict):
        return None
    resolved = raw.get("isResolved")
    if not isinstance(resolved, bool):
        return None
    comments_connection = raw.get("comments")
    raw_comments = (
        comments_connection.get("nodes", [])
        if isinstance(comments_connection, dict)
        else []
    )
    comments: list[ReviewComment] = []
    for raw_comment in raw_comments:
        if not isinstance(raw_comment, dict):
            continue
        author = raw_comment.get("author")
        login = author.get("login", "unknown") if isinstance(author, dict) else "unknown"
        body = raw_comment.get("body")
        node_id = raw_comment.get("id")
        if not isinstance(body, str) or not isinstance(node_id, str):
            continue
        comments.append(
            ReviewComment(
                node_id=node_id,
                database_id=_optional_int(raw_comment.get("fullDatabaseId")),
                author=str(login),
                body=body,
            )
        )

    node_id = raw.get("id")
    path = raw.get("path")
    side = raw.get("diffSide")
    if not isinstance(node_id, str) or not isinstance(path, str) or side not in {"LEFT", "RIGHT"}:
        return None
    return ReviewThread(
        node_id=node_id,
        path=path,
        side=side,
        line=_optional_int(raw.get("line")),
        original_line=_optional_int(raw.get("originalLine")),
        outdated=raw.get("isOutdated") is True,
        viewer_can_reply=raw.get("viewerCanReply") is True,
        comments=tuple(comments),
        resolved=resolved,
    )


_REVIEW_THREADS_QUERY = """
query ReviewThreads($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        nodes {
          id
          path
          diffSide
          line
          originalLine
          isResolved
          isOutdated
          viewerCanReply
          comments(first: 50) {
            nodes {
              id
              fullDatabaseId
              body
              author { login }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def _fetch_review_threads_page(
    owner: str,
    name: str,
    number: int,
    token: str,
    cursor: str | None,
) -> tuple[list[object], dict[str, object]]:
    response = _request_json(
        f"{GITHUB_API_URL}/graphql",
        "POST",
        token,
        {
            "query": _REVIEW_THREADS_QUERY,
            "variables": {
                "owner": owner,
                "name": name,
                "number": number,
                "cursor": cursor,
            },
        },
    )
    return _review_thread_page(response)


def _collect_resolved_threads(nodes: list[object], resolved: bool) -> list[ReviewThread]:
    collected: list[ReviewThread] = []
    for raw_thread in nodes:
        thread = _parse_thread(raw_thread)
        if thread is not None and thread.resolved is resolved:
            collected.append(thread)
    return collected


def _fetch_review_threads(
    repository: str,
    pr_number: str | int,
    token: str,
    *,
    resolved: bool,
    limit: int,
) -> list[ReviewThread]:
    owner, name = _repository_parts(repository)
    try:
        number = int(pr_number)
    except (TypeError, ValueError) as error:
        raise RuntimeError("PR_NUMBER must be an integer") from error

    cursor: str | None = None
    threads: list[ReviewThread] = []
    while len(threads) < limit:
        nodes, page_info = _fetch_review_threads_page(owner, name, number, token, cursor)
        for thread in _collect_resolved_threads(nodes, resolved):
            threads.append(thread)
            if len(threads) == limit:
                break
        if page_info.get("hasNextPage") is not True:
            break
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise GitHubRequestError("GitHub GraphQL pagination omitted endCursor")
        cursor = next_cursor
    return threads


def _review_thread_page(response: object) -> tuple[list[object], dict[str, object]]:
    if not isinstance(response, dict):
        raise GitHubRequestError("GitHub GraphQL returned an invalid response")  # pragma: no cover
    if response.get("errors"):
        raise GitHubRequestError(f"GitHub GraphQL returned errors: {str(response['errors'])[:500]}")  # pragma: no cover
    try:
        data = cast(dict[str, object], response["data"])
        repository = cast(dict[str, object], data["repository"])
        pull_request = cast(dict[str, object], repository["pullRequest"])
        connection = cast(dict[str, object], pull_request["reviewThreads"])
        nodes = connection["nodes"]
        page_info = connection["pageInfo"]
    except (KeyError, TypeError) as error:  # pragma: no cover
        raise GitHubRequestError("GitHub GraphQL response omitted reviewThreads") from error  # pragma: no cover
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise GitHubRequestError("GitHub GraphQL returned invalid reviewThreads data")  # pragma: no cover
    return nodes, page_info


def fetch_unresolved_review_threads(
    repository: str,
    pr_number: str | int,
    token: str,
) -> list[ReviewThread]:
    return _fetch_review_threads(
        repository,
        pr_number,
        token,
        resolved=False,
        limit=MAX_THREADS,
    )


def fetch_resolved_machine_threads(
    repository: str,
    pr_number: str | int,
    token: str,
) -> list[ReviewThread]:
    """Return already-resolved reviewer threads, used only to suppress repeats.

    These never reach a model prompt. A finding that duplicates one of them was
    settled on an earlier revision, so re-posting it would reopen a decision the
    owner already made.
    """
    threads = _fetch_review_threads(
        repository,
        pr_number,
        token,
        resolved=True,
        limit=MAX_RESOLVED_THREADS,
    )
    return [thread for thread in threads if is_machine_thread(thread) or _is_observable_review_thread(thread)]


def _is_observable_review_thread(thread: ReviewThread) -> bool:
    """Read-only deduplication identity; deliberately NOT an auto-resolution marker."""
    return bool(thread.comments and thread.comments[0].author == AUTOMATED_REVIEW_AUTHOR
                and re.search(r"<!-- observable-inline:[0-9a-f]{40}:", thread.comments[0].body))


def _rendered_comments(thread: ReviewThread) -> list[dict[str, object]]:
    selected = list(thread.comments[:1])
    if len(thread.comments) > 1:
        selected.extend(thread.comments[-(MAX_RENDERED_COMMENTS_PER_THREAD - 1) :])
    rendered: list[dict[str, object]] = []
    seen: set[str] = set()
    for comment in selected:
        if comment.node_id in seen:
            continue
        seen.add(comment.node_id)
        rendered.append(
            {
                "author": comment.author,
                "body": " ".join(strip_execution_metadata(comment.body).split())[:MAX_RENDERED_COMMENT_CHARACTERS],
            }
        )
    return rendered


def render_review_context(
    threads: list[ReviewThread] | tuple[ReviewThread, ...],
    paths: set[str] | frozenset[str] | None = None,
) -> str:
    rendered: list[dict[str, object]] = []
    for thread in threads:
        if paths is not None and thread.path not in paths:
            continue
        item = {
            "thread_id": thread.node_id,
            "reply_to_comment_id": thread.reply_to_comment_id,
            "path": thread.path,
            "side": thread.side,
            "line": thread.line,
            "original_line": thread.original_line,
            "outdated": thread.outdated,
            "viewer_can_reply": thread.viewer_can_reply,
            "comments": _rendered_comments(thread),
        }
        candidate = json.dumps([*rendered, item], ensure_ascii=False, indent=2)
        if len(candidate) > MAX_CONTEXT_CHARACTERS:
            break
        rendered.append(item)
    return json.dumps(rendered, ensure_ascii=False, indent=2)


def _semantic_tokens(text: str) -> set[str]:
    return {word for word in WORD_PATTERN.findall(text.lower()) if word not in STOP_WORDS}


def semantic_similarity(left: str, right: str) -> float:
    left_tokens = _semantic_tokens(left)
    right_tokens = _semantic_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))


def match_existing_thread(
    path: str,
    side: str,
    line: int,
    finding_text: str,
    threads: list[ReviewThread] | tuple[ReviewThread, ...],
) -> ThreadMatch | None:
    candidates: list[tuple[bool, float, ReviewThread]] = []
    for thread in threads:
        if thread.path != path:
            continue
        anchor_match = thread.line == line and thread.side == side
        similarity = semantic_similarity(finding_text, thread.searchable_text)
        if anchor_match or similarity >= SEMANTIC_DUPLICATE_THRESHOLD:
            candidates.append((anchor_match, similarity, thread))
    if not candidates:
        return None
    anchor_match, similarity, thread = max(candidates, key=lambda item: (item[0], item[1]))
    relationship = (
        "duplicate"
        if similarity >= SEMANTIC_DUPLICATE_THRESHOLD
        or (anchor_match and similarity >= ANCHOR_DUPLICATE_THRESHOLD)
        else "extension"
    )
    return ThreadMatch(thread=thread, relationship=relationship, similarity=similarity)


def reply_to_review_thread(
    repository: str,
    pr_number: str | int,
    token: str,
    comment_id: int,
    body: str,
) -> None:
    clean_body = body.strip()
    if not clean_body:
        raise RuntimeError("Reply body must not be empty")
    if len(clean_body) > MAX_REPLY_CHARACTERS:
        raise RuntimeError(f"Reply body exceeds {MAX_REPLY_CHARACTERS} characters")
    _request_json(
        f"{GITHUB_API_URL}/repos/{repository}/pulls/{pr_number}/comments/{comment_id}/replies",
        "POST",
        token,
        {"body": clean_body},
    )


def resolve_review_thread(token: str, thread_id: str) -> None:
    """Resolve an already validated machine-authored review thread."""
    response = _request_json(
        f"{GITHUB_API_URL}/graphql",
        "POST",
        token,
        {
            "query": (
                "mutation ResolveReviewThread($threadId: ID!) { "
                "resolveReviewThread(input: {threadId: $threadId}) { "
                "thread { isResolved } } }"
            ),
            "variables": {"threadId": thread_id},
        },
    )
    try:
        data = cast(dict[str, object], response["data"])
        mutation = cast(dict[str, object], data["resolveReviewThread"])
        thread = cast(dict[str, object], mutation["thread"])
        resolved = thread["isResolved"]
    except (KeyError, TypeError) as error:
        raise GitHubRequestError("GitHub did not confirm review-thread resolution") from error
    if resolved is not True:
        raise GitHubRequestError("GitHub did not resolve the review thread")


def machine_thread_head_sha(thread: ReviewThread) -> str | None:
    """Return the head SHA a reviewer bot stamped on the thread's first comment."""
    if not thread.comments:
        return None
    match = MACHINE_INLINE_MARKER.search(thread.comments[0].body)
    return match.group(1).lower() if match else None


def is_machine_thread(thread: ReviewThread) -> bool:
    """Whether a thread was opened by a reviewer bot and nobody else joined it.

    Human participation is the opt-out that matters: it means someone is using
    the thread, so no automated verdict may close it. The thread's own head SHA
    is not compared against the current one — auto-resolution exists precisely
    to close findings raised on an earlier revision.
    """
    if not thread.comments:
        return False
    return (
        thread.comments[0].author == AUTOMATED_REVIEW_AUTHOR
        and machine_thread_head_sha(thread) is not None
        and all(comment.author == AUTOMATED_REVIEW_AUTHOR for comment in thread.comments)
    )


def may_be_auto_fixed(thread: ReviewThread, head_sha: str, changed_paths: set[str]) -> bool:
    """Whether a `fixed` verdict on this thread can be true of this revision.

    Two deterministic conditions rule out the claim regardless of what a model
    asserts: the revision does not touch the file at all, or the thread was
    opened against this very revision, so nothing has changed since.
    """
    marked_head_sha = machine_thread_head_sha(thread)
    return thread.path in changed_paths and marked_head_sha != head_sha.lower()


def _required_commit_sha(name: str) -> str:
    value = _required_env(name)
    if not COMMIT_SHA_PATTERN.fullmatch(value):
        raise RuntimeError(f"{name} must be a full Git commit SHA")
    return value


def _run_git(*arguments: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", *arguments],
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout


def _changed_paths(base_sha: str, head_sha: str) -> set[str]:
    raw_paths = _run_git(
        "diff",
        "--name-only",
        "-z",
        base_sha,
        head_sha,
        "--",
        ".",
        text=False,
    )
    assert isinstance(raw_paths, bytes)
    return {
        path
        for path in raw_paths.decode("utf-8", errors="surrogateescape").split("\0")
        if path
    }


def changed_diff_lines(base_sha: str, head_sha: str, path: str) -> dict[str, set[int]]:
    if path not in _changed_paths(base_sha, head_sha):
        raise RuntimeError("Inline review path is not changed by this pull request")
    diff = _run_git(
        "diff",
        "--unified=0",
        "--no-ext-diff",
        base_sha,
        head_sha,
        "--",
        path,
    )
    assert isinstance(diff, str)
    changed_lines = {"LEFT": set(), "RIGHT": set()}
    old_line: int | None = None
    new_line: int | None = None
    for raw_line in diff.splitlines():
        hunk = HUNK_HEADER.match(raw_line)
        if hunk:
            old_line = int(hunk.group(1))
            new_line = int(hunk.group(2))
            continue
        if old_line is None or new_line is None:
            continue
        if raw_line.startswith("+"):
            changed_lines["RIGHT"].add(new_line)
            new_line += 1
        elif raw_line.startswith("-"):
            changed_lines["LEFT"].add(old_line)
            old_line += 1
        elif raw_line.startswith(" "):
            old_line += 1
            new_line += 1
    return changed_lines


def create_inline_comment(
    repository: str,
    pr_number: str | int,
    token: str,
    commit_id: str,
    path: str,
    side: str,
    line: int,
    body: str,
) -> None:
    clean_body = body.strip()
    if not clean_body:
        raise RuntimeError("Inline comment body must not be empty")
    if len(clean_body) > MAX_INLINE_COMMENT_CHARACTERS:
        raise RuntimeError(
            f"Inline comment body exceeds {MAX_INLINE_COMMENT_CHARACTERS} characters"
        )
    if side not in {"LEFT", "RIGHT"}:
        raise RuntimeError("Inline comment side must be LEFT or RIGHT")
    if isinstance(line, bool) or line < 1:
        raise RuntimeError("Inline comment line must be a positive integer")
    _request_json(
        f"{GITHUB_API_URL}/repos/{repository}/pulls/{pr_number}/comments",
        "POST",
        token,
        {
            "body": clean_body,
            "commit_id": commit_id,
            "path": path,
            "side": side,
            "line": line,
        },
    )


def _command_render() -> None:
    threads = fetch_unresolved_review_threads(
        _required_env("GITHUB_REPOSITORY"),
        _required_env("PR_NUMBER"),
        _github_token(),
    )
    print(render_review_context(threads))


def _command_reply(comment_id: int, body: str) -> None:
    repository = _required_env("GITHUB_REPOSITORY")
    pr_number = _required_env("PR_NUMBER")
    token = _github_token()
    threads = fetch_unresolved_review_threads(repository, pr_number, token)
    allowed_ids = {
        thread.reply_to_comment_id
        for thread in threads
        if thread.viewer_can_reply and thread.reply_to_comment_id is not None
    }
    if comment_id not in allowed_ids:
        raise RuntimeError("The requested comment is not a replyable unresolved review thread")
    reply_to_review_thread(repository, pr_number, token, comment_id, body)


def _clean_result_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _require_exact_keys(value: object, expected: frozenset[str], label: str) -> dict:
    if not isinstance(value, dict) or frozenset(value) != expected:
        raise RuntimeError(f"Claude review output has an invalid {label} object")
    return value


def _validate_claude_result_contract(result: object) -> dict:
    document = _require_exact_keys(
        result,
        frozenset({"summary", "findings", "thread_verdicts"}),
        "top-level",
    )
    if not isinstance(document["summary"], str) or not document["summary"].strip():
        raise RuntimeError("Claude review output has an invalid summary")
    findings = document["findings"]
    verdicts = document["thread_verdicts"]
    if not isinstance(findings, list) or len(findings) > 5:
        raise RuntimeError("Claude review output has an invalid findings array")
    if not isinstance(verdicts, list) or len(verdicts) > 200:
        raise RuntimeError("Claude review output has an invalid thread_verdicts array")

    finding_keys = frozenset(
        {"severity", "path", "side", "line", "title", "impact", "fix"}
    )
    for raw in findings:
        finding = _require_exact_keys(raw, finding_keys, "finding")
        line = finding["line"]
        if (
            finding["severity"] not in {"P1", "P2"}
            or finding["side"] not in {"LEFT", "RIGHT"}
            or isinstance(line, bool)
            or not isinstance(line, int)
            or line < 1
            or any(
                not isinstance(finding[field], str) or not finding[field].strip()
                for field in ("path", "title", "impact", "fix")
            )
        ):
            raise RuntimeError("Claude review output has an invalid finding value")

    verdict_keys = frozenset({"thread_id", "verdict", "reason"})
    for raw in verdicts:
        verdict = _require_exact_keys(raw, verdict_keys, "thread verdict")
        if (
            verdict["verdict"] not in {"confirmed", "fixed", "rejected", "needs_human"}
            or not isinstance(verdict["thread_id"], str)
            or not verdict["thread_id"].strip()
            or len(verdict["thread_id"]) > 200
            or not isinstance(verdict["reason"], str)
            or not verdict["reason"].strip()
        ):
            raise RuntimeError("Claude review output has an invalid thread verdict value")
    return document


def _parse_one_finding(
    raw: object,
    valid_paths: set[str],
    changed_lines: dict[str, dict[str, set[int]]],
    base_sha: str,
    head_sha: str,
    seen_locations: set[tuple[str, str, int]],
) -> ReviewFinding | None:
    """Validate one raw finding; return it, or None when it must be dropped."""
    if not isinstance(raw, dict):
        return None
    severity, path, side, line = (raw.get(key) for key in ("severity", "path", "side", "line"))
    if severity not in {"P1", "P2"} or not isinstance(path, str) or side not in {"LEFT", "RIGHT"}:
        return None
    if not (isinstance(line, int) and not isinstance(line, bool) and line >= 1):
        return None
    path = path[2:] if path.startswith("./") else path
    location = (path, side, line)
    if path not in valid_paths or location in seen_locations:
        return None
    if path not in changed_lines:
        changed_lines[path] = changed_diff_lines(base_sha, head_sha, path)
    if line not in changed_lines[path][side]:
        return None
    title = _clean_result_text(raw.get("title"), 160)
    impact = _clean_result_text(raw.get("impact"), 700)
    fix = _clean_result_text(raw.get("fix"), 700)
    if not all((title, impact, fix)):
        return None
    seen_locations.add(location)
    return ReviewFinding(severity, path, side, line, title, impact, fix)


def _parse_review_findings(raw_findings: list[object], base_sha: str, head_sha: str) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    seen_locations: set[tuple[str, str, int]] = set()
    changed_lines: dict[str, dict[str, set[int]]] = {}
    valid_paths = _changed_paths(base_sha, head_sha)
    for raw in raw_findings[:5]:
        finding = _parse_one_finding(raw, valid_paths, changed_lines, base_sha, head_sha, seen_locations)
        if finding is not None:
            findings.append(finding)
    return sorted(findings, key=lambda item: (0 if item.severity == "P1" else 1, item.path, item.line))


def _parse_thread_verdicts(raw_verdicts: list[object]) -> list[ThreadVerdict]:
    verdicts: list[ThreadVerdict] = []
    seen: set[str] = set()
    for raw in raw_verdicts[:200]:
        if not isinstance(raw, dict):
            continue
        thread_id = raw.get("thread_id")
        verdict = raw.get("verdict")
        reason = _clean_result_text(raw.get("reason"), 800)
        if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 200:
            continue
        if thread_id in seen or verdict not in {"confirmed", "fixed", "rejected", "needs_human"} or not reason:
            continue
        seen.add(thread_id)
        verdicts.append(ThreadVerdict(thread_id, verdict, reason))
    return verdicts


def _validated_claude_result(
    raw_result: str,
    base_sha: str,
    head_sha: str,
) -> tuple[str, list[ReviewFinding], list[ThreadVerdict]]:
    try:
        result = json.loads(raw_result)
    except json.JSONDecodeError as error:
        raise RuntimeError("Claude review output is not valid JSON") from error
    result = _validate_claude_result_contract(result)
    summary = _clean_result_text(result.get("summary"), MAX_CLAUDE_SUMMARY_CHARACTERS)
    raw_findings = result.get("findings")
    raw_verdicts = result.get("thread_verdicts")
    if not summary or not isinstance(raw_findings, list) or not isinstance(raw_verdicts, list):
        raise RuntimeError("Claude review output omitted summary, findings, or thread_verdicts")

    return summary, _parse_review_findings(raw_findings, base_sha, head_sha), _parse_thread_verdicts(raw_verdicts)


def _decode_execution_document(raw_output: str) -> list[object]:
    try:
        document = json.loads(raw_output)
    except json.JSONDecodeError:
        events = [json.loads(line) for line in raw_output.splitlines() if line.strip()]
        if not events:
            raise RuntimeError("Claude execution output contains no SDK events")
        return events
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in ("events", "messages"):
            nested = document.get(key)
            if isinstance(nested, list):
                return nested
        return [document]
    raise RuntimeError("Claude execution output has an invalid top-level shape")


def _claude_execution_events(execution_file: Path) -> list[object]:
    """Read bounded Claude SDK events from a JSON array or JSON-lines file."""
    execution_file = _validate_cli_path(execution_file, "execution file")
    try:
        size = execution_file.stat().st_size
    except OSError as error:
        raise RuntimeError("Claude execution output is unavailable") from error
    if size < 1 or size > MAX_CLAUDE_EXECUTION_FILE_BYTES:
        raise RuntimeError("Claude execution output has an invalid size")
    try:
        raw_output = execution_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError("Claude execution output could not be read as UTF-8") from error

    try:
        return _decode_execution_document(raw_output)
    except json.JSONDecodeError as error:
        raise RuntimeError("Claude execution output is not valid JSON") from error


def _result_event_text(event: dict[str, object]) -> str | None:
    if event.get("type") != "result":
        return None
    if event.get("is_error") is True:
        raise RuntimeError("Claude Code reported an unsuccessful result")
    result = event.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


def _assistant_event_text(event: dict[str, object]) -> str | None:
    if event.get("type") != "assistant":
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        text = "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ).strip()
        if text:
            return text
    return None


def _claude_final_response(execution_file: Path) -> str:
    """Extract the final text from Claude Code's execution event stream."""
    events = _claude_execution_events(execution_file)
    for event in reversed(events):
        if isinstance(event, dict):
            text = _result_event_text(event)
            if text is not None:
                return text

    # Older action/SDK combinations may omit the result text while retaining
    # the last assistant message. Accept text blocks only; tool payloads remain
    # untrusted execution data and are never interpreted as the final review.
    for event in reversed(events):
        if isinstance(event, dict):
            text = _assistant_event_text(event)
            if text is not None:
                return text
    raise RuntimeError("Claude execution output contains no final text response")


def _json_response_text(response: str) -> str:
    """Extract a schema-valid review object from an ordinary final response."""
    candidate = response.strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        _validate_claude_result_contract(parsed)
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))

    # Gateway-backed models sometimes add a short explanation or Markdown
    # fence despite the prompt. Decode every complete object boundary, accept
    # only objects satisfying the exact review contract, and use the last one
    # as the model's final answer. Surrounding text is never interpreted.
    decoder = json.JSONDecoder()
    valid_objects: list[dict] = []
    for match in re.finditer(r"\{", candidate):
        try:
            embedded, _ = decoder.raw_decode(candidate[match.start() :])
            document = _validate_claude_result_contract(embedded)
        except (json.JSONDecodeError, RuntimeError):
            continue
        valid_objects.append(document)
    if not valid_objects:
        raise RuntimeError("Claude review output contains no schema-valid JSON object")
    return json.dumps(valid_objects[-1], ensure_ascii=False, separators=(",", ":"))


def _normalized_claude_result(raw_result: str, base_sha: str, head_sha: str) -> str:
    summary, findings, verdicts = _validated_claude_result(
        _json_response_text(raw_result),
        base_sha,
        head_sha,
    )
    result = {
        "summary": summary,
        "findings": [
            {
                "severity": finding.severity,
                "path": finding.path,
                "side": finding.side,
                "line": finding.line,
                "title": finding.title,
                "impact": finding.impact,
                "fix": finding.fix,
            }
            for finding in findings
        ],
        "thread_verdicts": [
            {
                "thread_id": verdict.thread_id,
                "verdict": verdict.verdict,
                "reason": verdict.reason,
            }
            for verdict in verdicts
        ],
    }
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _sdk_content(event: object, event_type: str) -> list[dict]:
    if not isinstance(event, dict) or event.get("type") != event_type or event.get("parent_tool_use_id"):
        return []
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _is_diff_read_call(block: dict, diff_path: Path) -> bool:
    if block.get("type") != "tool_use" or block.get("name") != "Read" or not isinstance(block.get("id"), str):
        return False
    arguments = block.get("input")
    path = arguments.get("file_path") if isinstance(arguments, dict) else None
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    try:
        return Path(path).resolve() == diff_path
    except (OSError, ValueError, RuntimeError):
        return False


def _read_returned_lines(block: dict) -> bool:
    if block.get("is_error") not in (None, False):
        return False
    content = block.get("content")
    if isinstance(content, list):
        content = "\n".join(item["text"] for item in content if isinstance(item, dict)
                            and item.get("type") == "text" and isinstance(item.get("text"), str))
    # Read's cat -n style output uses an arrow or tab; accept colon-numbered
    # variants too. Empty pages and plain error/warning messages are not reads.
    return isinstance(content, str) and re.search(
        r"^[ \t]*[1-9]\d* *[→\t:] *\S", content, re.MULTILINE,
    ) is not None


def _track_diff_calls(event: dict, pending: set[str], diff_path: Path) -> None:
    for block in _sdk_content(event, "assistant"):
        call_id = block.get("id")
        if block.get("type") == "tool_use" and isinstance(call_id, str):
            pending.discard(call_id)  # Reused IDs must not retain earlier evidence.
            if _is_diff_read_call(block, diff_path):
                pending.add(call_id)


def _completed_diff_read(event: dict, pending: set[str]) -> bool:
    read = False
    for block in _sdk_content(event, "user"):
        call_id = block.get("tool_use_id")
        if block.get("type") == "tool_result" and isinstance(call_id, str) and call_id in pending:
            pending.remove(call_id)
            read = _read_returned_lines(block) or read
    return read


def _require_claude_diff_read(events: list[object]) -> None:
    diff_path = (Path.cwd() / ".ci-pr-review.diff").resolve()
    pending: set[str] = set()
    read = False
    for event in events:
        if not isinstance(event, dict) or event.get("parent_tool_use_id"):
            continue
        if event.get("type") == "result":
            break  # Tool events after the final result cannot justify that review.
        if event.get("type") == "system" and event.get("subtype") == "init":
            pending.clear()
            read = False
        _track_diff_calls(event, pending, diff_path)
        read = _completed_diff_read(event, pending) or read
    if not read:
        raise RuntimeError("diff_not_read: no successful Read of .ci-pr-review.diff before the final response")


def _command_extract(execution_file: Path, output_file: Path) -> None:
    output_file = _validate_cli_path(output_file, "output file")
    _require_claude_diff_read(_claude_execution_events(execution_file))
    normalized = _normalized_claude_result(
        _claude_final_response(execution_file),
        _required_commit_sha("BASE_SHA"),
        _required_commit_sha("HEAD_SHA"),
    )
    try:
        output_file.write_text(normalized + "\n", encoding="utf-8")
    except OSError as error:
        raise RuntimeError("Validated Claude review output could not be written") from error
    print("Extracted and validated Claude's final JSON review response.")


def _claude_review_result() -> str:
    result_file = os.environ.get("CLAUDE_REVIEW_RESULT_FILE", "").strip()
    if not result_file:
        return _required_env("CLAUDE_REVIEW_RESULT")
    path = Path(result_file)
    try:
        if path.stat().st_size > MAX_CLAUDE_REVIEW_RESULT_BYTES:
            raise RuntimeError("Validated Claude review result is too large")
        result = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError("Validated Claude review result could not be read") from error
    if not result:
        raise RuntimeError("Validated Claude review result is empty")
    return result


def _finding_text(finding: ReviewFinding) -> str:
    return f"{finding.title} {finding.impact} {finding.fix}"

def _is_unresolvable_inline_anchor(error: GitHubRequestError) -> bool:
    """Return whether GitHub rejected only an inline review anchor."""
    message = str(error)
    return "HTTP 422" in message and "could not be resolved" in message


def _render_review_summary(
    *,
    head_sha: str,
    summary: str,
    new_inline_findings: int,
    unanchored_findings: int,
    follow_ups: int,
    duplicate_findings: int,
    previously_settled_findings: int,
    confirmed_machine_findings: int,
    fixed_machine_findings: int,
    rejected_machine_findings: int,
    machine_findings_needing_human: int,
    changed_file_count: int,
) -> str:
    """Render the human-facing Claude review summary in scan-friendly sections."""
    action_required = any(
        (
            new_inline_findings,
            unanchored_findings,
            follow_ups,
            confirmed_machine_findings,
            machine_findings_needing_human,
        )
    )
    outcome = "Action required" if action_required else "No new actionable findings"
    execution = claude_execution_report(os.environ)
    lines = [
        f"## {CLAUDE_REVIEWER_LABEL}",
        "",
        technical_metadata(execution),
        "",
        "### Review scope",
        f"Complete base-to-head diff supplied · {changed_file_count} changed file(s). "
        "Chunking and tool pagination are internal execution details.",
        "",
        "### Review outcome",
        f"**{outcome}**",
        f"Reviewed head: `{head_sha}`",
        "",
        summary,
        "",
        "### Finding activity",
        "| Activity | Count |",
        "|---|---:|",
        f"| New inline findings | {new_inline_findings} |",
        f"| Findings without an inline anchor | {unanchored_findings} |",
        f"| Material follow-ups on existing threads | {follow_ups} |",
        f"| Existing findings not repeated | {duplicate_findings} |",
        f"| Resolved findings not re-raised | {previously_settled_findings} |",
        "",
        "### Existing machine-review threads",
        "| Status | Count |",
        "|---|---:|",
        f"| Confirmed and still open | {confirmed_machine_findings} |",
        f"| Fixed and auto-resolved | {fixed_machine_findings} |",
        f"| Rejected and auto-resolved | {rejected_machine_findings} |",
        f"| Waiting for human review | {machine_findings_needing_human} |",
    ]
    return "\n".join(lines)


def _review_already_posted(comments_url: str, marker: str, token: str) -> bool:
    """Return True when a prior review comment already carries the run marker."""
    # Page through every issue comment rather than trusting the marker to be
    # on the first 100: a busy PR can have far more comments than that, and
    # missing the marker on an older page would post a duplicate review.
    for page in range(1, 51):
        page_comments = _request_json(f"{comments_url}?per_page=100&page={page}", "GET", token)
        if not isinstance(page_comments, list):
            raise GitHubRequestError("GitHub returned an invalid issue-comment list")
        if any(
            marker in (comment.get("body") or "")
            for comment in page_comments
            if isinstance(comment, dict)
        ):
            return True
        if len(page_comments) < 100:
            return False
    return False


def _reply_if_not_posted(
    thread: ReviewThread,
    verdict_marker: str,
    body: str,
    repository: str,
    pr_number: str | int,
    token: str,
) -> None:
    if (
        thread.reply_to_comment_id is not None
        and thread.viewer_can_reply
        and not any(verdict_marker in comment.body for comment in thread.comments)
    ):
        reply_to_review_thread(
            repository,
            pr_number,
            token,
            thread.reply_to_comment_id,
            body,
        )


def _handle_confirmed_verdict(
    thread: ReviewThread,
    verdict_marker: str,
    verdict: ThreadVerdict,
    repository: str,
    pr_number: str | int,
    token: str,
    execution_footer: str,
) -> None:
    _reply_if_not_posted(
        thread,
        verdict_marker,
        f"{verdict_marker}\n**[{CLAUDE_REVIEWER_LABEL}] Finding remains valid**\n\n"
        f"Evidence: {verdict.reason}" + execution_footer,
        repository,
        pr_number,
        token,
    )
    print("  -> left open: confirmed still valid")


def _handle_needs_human_verdict(
    thread: ReviewThread,
    verdict_marker: str,
    verdict: ThreadVerdict,
    repository: str,
    pr_number: str | int,
    token: str,
    execution_footer: str,
) -> None:
    _reply_if_not_posted(
        thread,
        verdict_marker,
        f"{verdict_marker}\n**[{CLAUDE_REVIEWER_LABEL}] Human review requested**\n\n"
        f"Evidence: {verdict.reason}" + execution_footer,
        repository,
        pr_number,
        token,
    )
    print("  -> left for human review: needs_human")


def _auto_resolve_verdict(
    thread: ReviewThread,
    verdict_marker: str,
    verdict: ThreadVerdict,
    repository: str,
    pr_number: str | int,
    token: str,
    execution_footer: str,
    closed_thread_ids: set[str],
) -> str:
    """Resolve a fixed/rejected machine thread; return the outcome kind."""
    comment_id = thread.reply_to_comment_id
    if comment_id is None or not thread.viewer_can_reply:
        print("  -> skipped: thread has no repliable comment")
        return "skipped"
    heading = (
        "Resolved — the requested change is present in this revision"
        if verdict.verdict == "fixed"
        else "Rejected finding"
    )
    reply_to_review_thread(
        repository,
        pr_number,
        token,
        comment_id,
        f"{verdict_marker}\n**[{CLAUDE_REVIEWER_LABEL}] {heading}**\n\nReason: {verdict.reason}" + execution_footer,
    )
    resolve_review_thread(token, thread.node_id)
    closed_thread_ids.add(thread.node_id)
    print(f"  -> auto-resolved: {verdict.verdict}")
    return verdict.verdict


def _process_single_verdict(
    verdict: ThreadVerdict,
    threads_by_id: dict[str, ReviewThread],
    head_sha: str,
    changed_paths: set[str],
    repository: str,
    pr_number: str | int,
    token: str,
    execution_footer: str,
    closed_thread_ids: set[str],
) -> str:
    """Handle one thread verdict; return its outcome category."""
    thread = threads_by_id.get(verdict.thread_id)
    print(
        f"Thread verdict: id={verdict.thread_id} verdict={verdict.verdict} "
        f"reason={verdict.reason!r}"
    )
    # Only a reviewer-authored thread that no human has joined may be closed
    # automatically. Threads from earlier revisions qualify on purpose:
    # closing them after the fix lands is the point of this pass.
    if thread is None or not is_machine_thread(thread):
        print(f"  -> skipped: thread {verdict.thread_id} not a machine thread")
        return "skipped"
    verdict_marker = f"<!-- claude-thread-verdict:{head_sha}:{thread.node_id} -->"
    if verdict.verdict == "confirmed":
        _handle_confirmed_verdict(
            thread, verdict_marker, verdict, repository, pr_number, token, execution_footer
        )
        return "confirmed"
    if verdict.verdict == "fixed" and not may_be_auto_fixed(
        thread,
        head_sha,
        changed_paths,
    ):
        print("  -> downgraded to human review: fixed verdict not eligible for auto-resolve")
        return "needs_human"
    if verdict.verdict == "needs_human":
        _handle_needs_human_verdict(
            thread, verdict_marker, verdict, repository, pr_number, token, execution_footer
        )
        return "needs_human"
    if any(verdict_marker in comment.body for comment in thread.comments):
        print("  -> skipped: verdict already posted for this head SHA")
        return "skipped"
    return _auto_resolve_verdict(
        thread, verdict_marker, verdict, repository, pr_number, token,
        execution_footer, closed_thread_ids,
    )


def _process_thread_verdicts(
    verdicts: list[ThreadVerdict],
    threads_by_id: dict[str, ReviewThread],
    head_sha: str,
    changed_paths: set[str],
    repository: str,
    pr_number: str | int,
    token: str,
    execution_footer: str,
) -> tuple[int, int, int, int, set[str]]:
    confirmed = 0
    fixed = 0
    rejected = 0
    needing_human = 0
    closed_thread_ids: set[str] = set()
    for verdict in verdicts:
        outcome = _process_single_verdict(
            verdict, threads_by_id, head_sha, changed_paths,
            repository, pr_number, token, execution_footer, closed_thread_ids,
        )
        if outcome == "confirmed":
            confirmed += 1
        elif outcome == "fixed":
            fixed += 1
        elif outcome == "rejected":
            rejected += 1
        elif outcome == "needs_human":
            needing_human += 1
    return confirmed, fixed, rejected, needing_human, closed_thread_ids


def _categorize_findings(
    findings: list[ReviewFinding],
    settled_threads: list[ReviewThread],
    threads: list[ReviewThread],
    closed_thread_ids: set[str],
) -> tuple[
    list[ReviewFinding],
    list[tuple[ReviewFinding, ReviewThread]],
    list[ReviewFinding],
    list[ReviewFinding],
]:
    new_findings: list[ReviewFinding] = []
    follow_ups: list[tuple[ReviewFinding, ReviewThread]] = []
    duplicates: list[ReviewFinding] = []
    previously_settled: list[ReviewFinding] = []
    for finding in findings:
        settled = match_existing_thread(
            finding.path,
            finding.side,
            finding.line,
            _finding_text(finding),
            settled_threads,
        )
        if settled is not None and settled.relationship == "duplicate":
            previously_settled.append(finding)
            continue
        match = match_existing_thread(
            finding.path,
            finding.side,
            finding.line,
            _finding_text(finding),
            [thread for thread in threads if thread.node_id not in closed_thread_ids],
        )
        if match is None:
            new_findings.append(finding)
        elif match.relationship == "duplicate":
            duplicates.append(finding)
        elif match.thread.viewer_can_reply and match.thread.reply_to_comment_id is not None:
            follow_ups.append((finding, match.thread))
        else:
            new_findings.append(finding)
    return new_findings, follow_ups, duplicates, previously_settled


def _post_findings_and_follow_ups(
    new_findings: list[ReviewFinding],
    follow_ups: list[tuple[ReviewFinding, ReviewThread]],
    repository: str,
    pr_number: str | int,
    token: str,
    head_sha: str,
    execution_footer: str,
) -> tuple[list[ReviewFinding], list[ReviewFinding]]:
    unanchored_findings: list[ReviewFinding] = []
    for finding in new_findings:
        location_digest = hashlib.sha256(
            f"{finding.path}:{finding.side}:{finding.line}".encode("utf-8")
        ).hexdigest()[:16]
        inline_marker = (
            f"<!-- claude-inline:{head_sha}:{location_digest} -->"
        )
        try:
            create_inline_comment(
                repository,
                pr_number,
                token,
                head_sha,
                finding.path,
                finding.side,
                finding.line,
                (
                    f"{inline_marker}\n**[{CLAUDE_REVIEWER_LABEL}] "
                    f"{finding.severity} — {finding.title}**\n\n"
                    f"Impact: {finding.impact}\n\nProposed fix: {finding.fix}"
                    + execution_footer
                ),
            )
        except GitHubRequestError as error:
            if not _is_unresolvable_inline_anchor(error):
                raise
            unanchored_findings.append(finding)
            print(
                "GitHub rejected the inline review anchor for "
                f"{finding.path}:{finding.line}; publishing it in the review summary."
            )

    posted_follow_ups: list[ReviewFinding] = []
    for finding, thread in follow_ups:
        comment_id = thread.reply_to_comment_id
        assert comment_id is not None
        follow_up_marker = f"<!-- claude-thread-followup:{head_sha}:{thread.node_id} -->"
        if any(follow_up_marker in comment.body for comment in thread.comments):
            continue
        reply_to_review_thread(
            repository,
            pr_number,
            token,
            comment_id,
            (
                f"{follow_up_marker}\n**[{CLAUDE_REVIEWER_LABEL}] "
                f"Additional evidence — {finding.severity}: "
                f"{finding.title}**\n\nImpact: {finding.impact}\n\n"
                f"Proposed fix: {finding.fix}"
                + execution_footer
            ),
        )
        posted_follow_ups.append(finding)
    return unanchored_findings, posted_follow_ups


def _command_publish() -> None:
    repository = _required_env("GITHUB_REPOSITORY")
    pr_number = _required_env("PR_NUMBER")
    base_sha = _required_commit_sha("BASE_SHA")
    head_sha = _required_commit_sha("HEAD_SHA")
    token = _github_token()
    model = _clean_result_text(_required_env("CLAUDE_REVIEW_MODEL"), 200)
    execution = claude_execution_report({**os.environ, "CLAUDE_REVIEW_MODEL": model})
    execution_footer = execution.footer("review")
    run_id = _required_env("CLAUDE_REVIEW_RUN_ID")
    if not run_id.isdigit():
        raise RuntimeError("CLAUDE_REVIEW_RUN_ID must be numeric")
    marker = f"<!-- claude-pr-review:{head_sha}:{run_id} -->"
    comments_url = f"{GITHUB_API_URL}/repos/{repository}/issues/{pr_number}/comments"
    if _review_already_posted(comments_url, marker, token):
        print("The Claude review already exists for this run; skipping duplicate.")
        return

    summary, findings, verdicts = _validated_claude_result(
        _claude_review_result(),
        base_sha,
        head_sha,
    )
    threads = fetch_unresolved_review_threads(repository, pr_number, token)
    settled_threads = fetch_resolved_machine_threads(repository, pr_number, token)
    changed_paths = _changed_paths(base_sha, head_sha)
    threads_by_id = {thread.node_id: thread for thread in threads}
    (
        confirmed_direct_findings,
        fixed_direct_findings,
        rejected_direct_findings,
        direct_findings_needing_human,
        closed_thread_ids,
    ) = _process_thread_verdicts(
        verdicts,
        threads_by_id,
        head_sha,
        changed_paths,
        repository,
        pr_number,
        token,
        execution_footer,
    )
    new_findings, follow_ups, duplicates, previously_settled = _categorize_findings(
        findings,
        settled_threads,
        threads,
        closed_thread_ids,
    )
    unanchored_findings, posted_follow_ups = _post_findings_and_follow_ups(
        new_findings,
        follow_ups,
        repository,
        pr_number,
        token,
        head_sha,
        execution_footer,
    )

    lines = _render_review_summary(
        head_sha=head_sha,
        summary=summary,
        new_inline_findings=len(new_findings) - len(unanchored_findings),
        unanchored_findings=len(unanchored_findings),
        follow_ups=len(posted_follow_ups),
        duplicate_findings=len(duplicates),
        previously_settled_findings=len(previously_settled),
        confirmed_machine_findings=confirmed_direct_findings,
        fixed_machine_findings=fixed_direct_findings,
        rejected_machine_findings=rejected_direct_findings,
        machine_findings_needing_human=direct_findings_needing_human,
        changed_file_count=len(changed_paths),
    ).splitlines()
    if new_findings:
        lines.extend(["", "New findings:"])
        lines.extend(
            f"- **{finding.severity} — `{finding.path}:{finding.line}`**: {finding.title}."
            for finding in new_findings
        )
    if unanchored_findings:
        lines.extend(["", "Findings without inline anchors:"])
        lines.extend(
            f"- **{finding.severity} — `{finding.path}:{finding.line}`**: "
            f"{finding.title}. GitHub rejected this diff anchor. "
            f"Impact: {finding.impact} Proposed fix: {finding.fix}"
            for finding in unanchored_findings
        )
    if previously_settled:
        # Listed, not just counted: an identical finding on an already-resolved
        # thread is either noise or a regression, and only the owner can tell.
        lines.extend(["", "Repeats of already-resolved threads, suppressed:"])
        lines.extend(
            f"- **{finding.severity} — `{finding.path}:{finding.line}`**: {finding.title}."
            for finding in previously_settled
        )
    if posted_follow_ups:
        lines.extend(["", "Material additions to existing threads:"])
        lines.extend(
            f"- **{finding.severity} — `{finding.path}:{finding.line}`**: {finding.title}."
            for finding in posted_follow_ups
        )
    if execution.details():
        lines.extend(["", execution.details()])
    lines.extend(["", marker])
    _request_json(comments_url, "POST", token, {"body": "\n".join(lines)})
    print(
        "Published the validated Claude review with "
        f"{len(new_findings)} inline finding(s), {len(posted_follow_ups)} follow-up(s), "
        f"{len(duplicates)} duplicate(s) suppressed, "
        f"{len(previously_settled)} repeat(s) of resolved threads suppressed, and "
        f"{fixed_direct_findings + rejected_direct_findings} thread(s) auto-resolved."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("render", help="Render unresolved review threads as bounded JSON")
    reply_parser = subparsers.add_parser("reply", help="Reply to a validated unresolved thread")
    reply_parser.add_argument("--comment-id", required=True, type=int)
    reply_parser.add_argument("--body", required=True)
    subparsers.add_parser(
        "publish",
        help="Validate and publish the structured Claude review result",
    )
    extract_parser = subparsers.add_parser(
        "extract",
        help="Extract and validate JSON from a Claude Code execution output file",
    )
    extract_parser.add_argument("--execution-file", required=True, type=Path)
    extract_parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    if arguments.command == "render":
        _command_render()
    elif arguments.command == "reply":
        _command_reply(arguments.comment_id, arguments.body)
    elif arguments.command == "extract":
        _command_extract(arguments.execution_file, arguments.output)
    else:
        _command_publish()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"PR review context failed: {error}", file=sys.stderr)
        sys.exit(1)

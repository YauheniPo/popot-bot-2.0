#!/usr/bin/env python3
"""Load unresolved GitHub review threads and safely reply to them."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request


GITHUB_API_URL = "https://api.github.com"
MAX_THREADS = 200
MAX_CONTEXT_CHARACTERS = 24_000
MAX_RENDERED_COMMENTS_PER_THREAD = 3
MAX_RENDERED_COMMENT_CHARACTERS = 1_000
MAX_REPLY_CHARACTERS = 4_000
MAX_INLINE_COMMENT_CHARACTERS = 4_000
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
SEMANTIC_DUPLICATE_THRESHOLD = 0.42
ANCHOR_DUPLICATE_THRESHOLD = 0.24
WORD_PATTERN = re.compile(r"[a-zа-яё0-9_]{4,}", re.IGNORECASE)
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

    @property
    def reply_to_comment_id(self) -> int | None:
        return self.comments[0].database_id if self.comments else None

    @property
    def searchable_text(self) -> str:
        return " ".join(comment.body for comment in self.comments)


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


class GitHubRequestError(RuntimeError):
    pass


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is missing")
    return value


def _github_token() -> str:
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
) -> object:
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
    if not isinstance(raw, dict) or raw.get("isResolved") is not False:
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
    )


def fetch_unresolved_review_threads(
    repository: str,
    pr_number: str | int,
    token: str,
) -> list[ReviewThread]:
    owner, name = _repository_parts(repository)
    try:
        number = int(pr_number)
    except (TypeError, ValueError) as error:
        raise RuntimeError("PR_NUMBER must be an integer") from error

    query = """
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
    cursor: str | None = None
    threads: list[ReviewThread] = []
    while len(threads) < MAX_THREADS:
        response = _request_json(
            f"{GITHUB_API_URL}/graphql",
            "POST",
            token,
            {
                "query": query,
                "variables": {
                    "owner": owner,
                    "name": name,
                    "number": number,
                    "cursor": cursor,
                },
            },
        )
        if not isinstance(response, dict):
            raise GitHubRequestError("GitHub GraphQL returned an invalid response")
        errors = response.get("errors")
        if errors:
            raise GitHubRequestError(f"GitHub GraphQL returned errors: {str(errors)[:500]}")
        try:
            connection = response["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
        except (KeyError, TypeError) as error:
            raise GitHubRequestError("GitHub GraphQL response omitted reviewThreads") from error
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise GitHubRequestError("GitHub GraphQL returned invalid reviewThreads data")
        for raw_thread in nodes:
            thread = _parse_thread(raw_thread)
            if thread is not None:
                threads.append(thread)
                if len(threads) == MAX_THREADS:
                    break
        if page_info.get("hasNextPage") is not True:
            break
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise GitHubRequestError("GitHub GraphQL pagination omitted endCursor")
        cursor = next_cursor
    return threads


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
                "body": " ".join(comment.body.split())[:MAX_RENDERED_COMMENT_CHARACTERS],
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


def _validated_claude_result(
    raw_result: str,
    base_sha: str,
    head_sha: str,
) -> tuple[str, list[ReviewFinding]]:
    try:
        result = json.loads(raw_result)
    except json.JSONDecodeError as error:
        raise RuntimeError("Claude review output is not valid JSON") from error
    if not isinstance(result, dict):
        raise RuntimeError("Claude review output must be a JSON object")
    summary = _clean_result_text(result.get("summary"), 1_000)
    raw_findings = result.get("findings")
    if not summary or not isinstance(raw_findings, list):
        raise RuntimeError("Claude review output omitted summary or findings")

    findings: list[ReviewFinding] = []
    seen_locations: set[tuple[str, str, int]] = set()
    changed_lines_by_path: dict[str, dict[str, set[int]]] = {}
    valid_paths = _changed_paths(base_sha, head_sha)
    for raw in raw_findings[:5]:
        if not isinstance(raw, dict):
            continue
        severity = raw.get("severity")
        path = raw.get("path")
        side = raw.get("side")
        line = raw.get("line")
        if (
            severity not in {"P1", "P2"}
            or not isinstance(path, str)
            or side not in {"LEFT", "RIGHT"}
            or isinstance(line, bool)
            or not isinstance(line, int)
            or line < 1
        ):
            continue
        if path.startswith("./"):
            path = path[2:]
        if path not in valid_paths:
            continue
        location = (path, side, line)
        if location in seen_locations:
            continue
        if path not in changed_lines_by_path:
            changed_lines_by_path[path] = changed_diff_lines(base_sha, head_sha, path)
        if line not in changed_lines_by_path[path][side]:
            continue
        title = _clean_result_text(raw.get("title"), 160)
        impact = _clean_result_text(raw.get("impact"), 700)
        fix = _clean_result_text(raw.get("fix"), 700)
        if not all((title, impact, fix)):
            continue
        seen_locations.add(location)
        findings.append(ReviewFinding(severity, path, side, line, title, impact, fix))
    findings.sort(key=lambda item: (0 if item.severity == "P1" else 1, item.path, item.line))
    return summary, findings


def _finding_text(finding: ReviewFinding) -> str:
    return f"{finding.title} {finding.impact} {finding.fix}"


def _command_publish() -> None:
    repository = _required_env("GITHUB_REPOSITORY")
    pr_number = _required_env("PR_NUMBER")
    base_sha = _required_commit_sha("BASE_SHA")
    head_sha = _required_commit_sha("HEAD_SHA")
    token = _github_token()
    model = _clean_result_text(_required_env("CLAUDE_REVIEW_MODEL"), 200)
    run_id = _required_env("CLAUDE_REVIEW_RUN_ID")
    if not run_id.isdigit():
        raise RuntimeError("CLAUDE_REVIEW_RUN_ID must be numeric")
    marker = f"<!-- claude-pr-review:{head_sha}:{run_id} -->"
    comments_url = f"{GITHUB_API_URL}/repos/{repository}/issues/{pr_number}/comments"
    existing_comments = _request_json(f"{comments_url}?per_page=100", "GET", token)
    if not isinstance(existing_comments, list):
        raise GitHubRequestError("GitHub returned an invalid issue-comment list")
    if any(
        marker in (comment.get("body") or "")
        for comment in existing_comments
        if isinstance(comment, dict)
    ):
        print("The Claude review already exists for this run; skipping duplicate.")
        return

    summary, findings = _validated_claude_result(
        _required_env("CLAUDE_REVIEW_RESULT"),
        base_sha,
        head_sha,
    )
    threads = fetch_unresolved_review_threads(repository, pr_number, token)
    new_findings: list[ReviewFinding] = []
    follow_ups: list[tuple[ReviewFinding, ReviewThread]] = []
    duplicates: list[ReviewFinding] = []
    for finding in findings:
        match = match_existing_thread(
            finding.path,
            finding.side,
            finding.line,
            _finding_text(finding),
            threads,
        )
        if match is None:
            new_findings.append(finding)
        elif match.relationship == "duplicate":
            duplicates.append(finding)
        elif match.thread.viewer_can_reply and match.thread.reply_to_comment_id is not None:
            follow_ups.append((finding, match.thread))
        else:
            new_findings.append(finding)

    for finding in new_findings:
        location_digest = hashlib.sha256(
            f"{finding.path}:{finding.side}:{finding.line}".encode("utf-8")
        ).hexdigest()[:16]
        inline_marker = (
            f"<!-- claude-inline:{head_sha}:{location_digest} -->"
        )
        create_inline_comment(
            repository,
            pr_number,
            token,
            head_sha,
            finding.path,
            finding.side,
            finding.line,
            (
                f"{inline_marker}\n**{finding.severity} — {finding.title}**\n\n"
                f"Impact: {finding.impact}\n\nProposed fix: {finding.fix}"
            ),
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
                f"{follow_up_marker}\n**Additional evidence — {finding.severity}: "
                f"{finding.title}**\n\nImpact: {finding.impact}\n\n"
                f"Proposed fix: {finding.fix}"
            ),
        )
        posted_follow_ups.append(finding)

    lines = [
        "## Claude Code review",
        "",
        f"> Provider: OpenRouter · Model: `{model}`",
        f"> Reviewed Head SHA: `{head_sha}`",
        "",
        f"Summary: {summary}",
        "",
        f"New inline findings: {len(new_findings)}.",
        f"Material thread follow-ups: {len(posted_follow_ups)}.",
        f"Existing unresolved findings not repeated: {len(duplicates)}.",
    ]
    if not new_findings and not posted_follow_ups:
        lines.extend(["", "Findings: No new actionable findings."])
    if new_findings:
        lines.extend(["", "New findings:"])
        lines.extend(
            f"- **{finding.severity} — `{finding.path}:{finding.line}`**: {finding.title}."
            for finding in new_findings
        )
    if posted_follow_ups:
        lines.extend(["", "Material additions to existing threads:"])
        lines.extend(
            f"- **{finding.severity} — `{finding.path}:{finding.line}`**: {finding.title}."
            for finding in posted_follow_ups
        )
    lines.extend(["", marker])
    _request_json(comments_url, "POST", token, {"body": "\n".join(lines)})
    print(
        "Published the validated Claude review with "
        f"{len(new_findings)} inline finding(s), {len(posted_follow_ups)} follow-up(s), "
        f"and {len(duplicates)} duplicate(s) suppressed."
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
    arguments = parser.parse_args()
    if arguments.command == "render":
        _command_render()
    elif arguments.command == "reply":
        _command_reply(arguments.comment_id, arguments.body)
    else:
        _command_publish()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"PR review context failed: {error}", file=sys.stderr)
        sys.exit(1)

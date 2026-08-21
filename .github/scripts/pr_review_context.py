#!/usr/bin/env python3
"""Load unresolved GitHub review threads and safely reply to them."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import re
import sys
import urllib.error
import urllib.request


GITHUB_API_URL = "https://api.github.com"
MAX_THREADS = 200
MAX_CONTEXT_CHARACTERS = 24_000
MAX_RENDERED_COMMENTS_PER_THREAD = 3
MAX_RENDERED_COMMENT_CHARACTERS = 1_000
MAX_REPLY_CHARACTERS = 4_000
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("render", help="Render unresolved review threads as bounded JSON")
    reply_parser = subparsers.add_parser("reply", help="Reply to a validated unresolved thread")
    reply_parser.add_argument("--comment-id", required=True, type=int)
    reply_parser.add_argument("--body", required=True)
    arguments = parser.parse_args()
    if arguments.command == "render":
        _command_render()
    else:
        _command_reply(arguments.comment_id, arguments.body)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"PR review context failed: {error}", file=sys.stderr)
        sys.exit(1)

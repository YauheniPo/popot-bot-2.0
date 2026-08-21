#!/usr/bin/env python3
"""Review a pull request through OpenRouter and publish a formal GitHub review."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request


MAX_CHUNK_CHARACTERS = 48_000
MAX_REVIEW_CHUNKS = 12
MAX_PARALLEL_REQUESTS = 3
MAX_REQUEST_ATTEMPTS = 3
MAX_OUTPUT_TOKENS = 1_400
MAX_FINDINGS = 5
MAX_RENDERED_LINE_CHARACTERS = 4_000
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GITHUB_API_URL = "https://api.github.com"
REVIEW_RULES_PATH = Path(".github/REVIEWER.md")
EXCLUDED_REVIEW_PATHS = frozenset(
    {
        ".github/workflows/claude-review.yml",
        ".github/scripts/openrouter_pr_review.py",
        str(REVIEW_RULES_PATH),
    }
)
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
REVIEW_RESPONSE_SCHEMA = {
    "name": "pull_request_review_chunk",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "findings"],
        "properties": {
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "severity",
                        "path",
                        "side",
                        "line",
                        "title",
                        "impact",
                        "fix",
                    ],
                    "properties": {
                        "severity": {"type": "string", "enum": ["P1", "P2"]},
                        "path": {"type": "string"},
                        "side": {"type": "string", "enum": ["RIGHT", "LEFT"]},
                        "line": {"type": "integer", "minimum": 1},
                        "title": {"type": "string"},
                        "impact": {"type": "string"},
                        "fix": {"type": "string"},
                    },
                },
            },
        },
    },
}


@dataclass(frozen=True)
class ReviewFile:
    path: str
    rendered_diff: str
    right_lines: frozenset[int]
    left_lines: frozenset[int]
    shortened: bool = False


@dataclass(frozen=True)
class DiffSegment:
    path: str
    text: str


@dataclass(frozen=True)
class ReviewChunk:
    text: str
    paths: frozenset[str]
    segment_paths: tuple[str, ...]


@dataclass(frozen=True)
class ReviewPlan:
    chunks: tuple[ReviewChunk, ...]
    total_files: int
    complete_files: frozenset[str]
    partial_files: frozenset[str]
    omitted_files: frozenset[str]

    @property
    def complete(self) -> bool:
        return not self.partial_files and not self.omitted_files


@dataclass(frozen=True)
class Finding:
    severity: str
    path: str
    side: str
    line: int
    title: str
    impact: str
    fix: str


class RequestError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


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
        raise RequestError(
            f"{method} {url} failed with HTTP {error.code}: {details[:500]}",
            status=error.code,
        ) from error
    except urllib.error.URLError as error:
        raise RequestError(f"{method} {url} failed before receiving a response") from error


def run_git(*arguments: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", *arguments],
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout


def changed_paths(base_sha: str, head_sha: str) -> list[str]:
    raw_paths = run_git("diff", "--name-only", "-z", base_sha, head_sha, "--", ".", text=False)
    assert isinstance(raw_paths, bytes)
    paths = raw_paths.decode("utf-8", errors="surrogateescape").split("\0")
    return [path for path in paths if path and path not in EXCLUDED_REVIEW_PATHS]


def _bounded_line(line: str) -> tuple[str, bool]:
    if len(line) <= MAX_RENDERED_LINE_CHARACTERS:
        return line, False
    return f"{line[:MAX_RENDERED_LINE_CHARACTERS]}… [line shortened by reviewer]", True


def annotate_diff(path: str, diff: str) -> ReviewFile:
    """Add stable left/right line labels while preserving the authoritative diff text."""
    rendered: list[str] = []
    right_lines: set[int] = set()
    left_lines: set[int] = set()
    old_line: int | None = None
    new_line: int | None = None
    shortened = False

    for raw_line in diff.splitlines():
        hunk = HUNK_HEADER.match(raw_line)
        if hunk:
            old_line = int(hunk.group(1))
            new_line = int(hunk.group(2))
            rendered.append(raw_line)
            continue

        if old_line is None or new_line is None:
            bounded, was_shortened = _bounded_line(raw_line)
            shortened |= was_shortened
            rendered.append(f"META|{bounded}")
            continue

        prefix = raw_line[:1]
        content, was_shortened = _bounded_line(raw_line[1:] if raw_line else "")
        shortened |= was_shortened
        if prefix == "+":
            right_lines.add(new_line)
            rendered.append(f"RIGHT {new_line}|+{content}")
            new_line += 1
        elif prefix == "-":
            left_lines.add(old_line)
            rendered.append(f"LEFT {old_line}|-{content}")
            old_line += 1
        elif prefix == " ":
            rendered.append(f"CONTEXT L{old_line}/R{new_line}| {content}")
            old_line += 1
            new_line += 1
        else:
            rendered.append(f"META|{raw_line}")

    return ReviewFile(
        path=path,
        rendered_diff="\n".join(rendered),
        right_lines=frozenset(right_lines),
        left_lines=frozenset(left_lines),
        shortened=shortened,
    )


def read_review_files(base_sha: str, head_sha: str) -> list[ReviewFile]:
    review_files: list[ReviewFile] = []
    for path in changed_paths(base_sha, head_sha):
        diff = run_git("diff", "--unified=3", "--no-ext-diff", base_sha, head_sha, "--", path)
        assert isinstance(diff, str)
        if diff:
            review_files.append(annotate_diff(path, diff))
    return review_files


def _review_priority(review_file: ReviewFile) -> tuple[int, str]:
    path = review_file.path.lower()
    suffix = Path(path).suffix
    if suffix in {".md", ".rst", ".txt"}:
        return 2, path
    if "/test" in path or Path(path).name.startswith("test_"):
        return 1, path
    return 0, path


def split_file(review_file: ReviewFile) -> list[DiffSegment]:
    lines = review_file.rendered_diff.splitlines()
    fixed_header_size = len(review_file.path) + 120
    parts: list[list[str]] = []
    current: list[str] = []
    current_size = fixed_header_size
    for line in lines:
        line_size = len(line) + 1
        if current and current_size + line_size > MAX_CHUNK_CHARACTERS:
            parts.append(current)
            current = []
            current_size = fixed_header_size
        current.append(line)
        current_size += line_size
    if current or not parts:
        parts.append(current)

    total_parts = len(parts)
    return [
        DiffSegment(
            path=review_file.path,
            text=(
                f"===== FILE {review_file.path} · PART {index}/{total_parts} =====\n"
                "Only RIGHT/LEFT labelled lines are changed and eligible for findings.\n"
                + "\n".join(part)
            ),
        )
        for index, part in enumerate(parts, start=1)
    ]


def _pack_chunk(segments: list[DiffSegment]) -> ReviewChunk:
    return ReviewChunk(
        text="\n\n".join(item.text for item in segments),
        paths=frozenset(item.path for item in segments),
        segment_paths=tuple(item.path for item in segments),
    )


def build_review_plan(review_files: list[ReviewFile]) -> ReviewPlan:
    ordered_files = sorted(review_files, key=_review_priority)
    all_segments = [segment for review_file in ordered_files for segment in split_file(review_file)]
    chunks: list[ReviewChunk] = []
    chunk_segments: list[DiffSegment] = []
    chunk_size = 0

    for segment in all_segments:
        separator_size = 2 if chunk_segments else 0
        if chunk_segments and chunk_size + separator_size + len(segment.text) > MAX_CHUNK_CHARACTERS:
            chunks.append(_pack_chunk(chunk_segments))
            chunk_segments = []
            chunk_size = 0
            separator_size = 0
        chunk_segments.append(segment)
        chunk_size += separator_size + len(segment.text)
    if chunk_segments:
        chunks.append(_pack_chunk(chunk_segments))

    selected_chunks = chunks[:MAX_REVIEW_CHUNKS]
    included_segments = Counter(path for chunk in selected_chunks for path in chunk.segment_paths)
    total_segments = Counter(segment.path for segment in all_segments)
    shortened_paths = {review_file.path for review_file in review_files if review_file.shortened}
    complete_files = {
        path
        for path, count in total_segments.items()
        if included_segments[path] == count and path not in shortened_paths
    }
    partial_files = {
        path
        for path, count in included_segments.items()
        if count < total_segments[path] or path in shortened_paths
    }
    omitted_files = set(total_segments) - set(included_segments)
    return ReviewPlan(
        chunks=tuple(selected_chunks),
        total_files=len(review_files),
        complete_files=frozenset(complete_files),
        partial_files=frozenset(partial_files),
        omitted_files=frozenset(omitted_files),
    )


def read_review_rules() -> str:
    try:
        rules = REVIEW_RULES_PATH.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(f"Could not read reviewer rules from {REVIEW_RULES_PATH}") from error
    if not rules:
        raise RuntimeError(f"Reviewer rules file {REVIEW_RULES_PATH} is empty")
    return rules


def _response_content(response: object) -> str:
    try:
        content = response["choices"][0]["message"]["content"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("OpenRouter response did not contain a review message") from error
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("OpenRouter returned an empty review message")
    return content.strip()


def parse_review_response(response: object) -> dict[str, object]:
    content = _response_content(response)
    if content.startswith("```") and content.endswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        raise RuntimeError("OpenRouter returned malformed structured review JSON") from error
    if not isinstance(parsed, dict) or not isinstance(parsed.get("findings"), list):
        raise RuntimeError("OpenRouter structured review did not match the expected object shape")
    return parsed


def review_chunk(
    api_key: str,
    model: str,
    chunk: ReviewChunk,
    chunk_number: int,
    total_chunks: int,
) -> dict[str, object]:
    user_prompt = f"""Review chunk {chunk_number} of {total_chunks} from one pull request.

The diff is annotated with exact GitHub coordinates. A line labelled `RIGHT 42|+` is
new line 42; `LEFT 17|-` is deleted line 17. Context and META lines cannot receive a
finding. Return only the JSON object required by the response schema.

{chunk.text}
"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com",
        "X-Title": "popot-bot-2.0 PR review",
    }
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "provider": {"require_parameters": True},
        "response_format": {"type": "json_schema", "json_schema": REVIEW_RESPONSE_SCHEMA},
        "messages": [
            {"role": "system", "content": read_review_rules()},
            {"role": "user", "content": user_prompt},
        ],
    }
    for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
        try:
            response = request_json(OPENROUTER_URL, "POST", headers, body)
            break
        except RequestError as error:
            retryable = error.status is None or error.status in RETRYABLE_HTTP_STATUSES
            if not retryable or attempt == MAX_REQUEST_ATTEMPTS:
                raise
            time.sleep(2 ** (attempt - 1))
    return parse_review_response(response)


def review_chunks(api_key: str, model: str, chunks: tuple[ReviewChunk, ...]) -> list[dict[str, object]]:
    if not chunks:
        return []
    results: dict[int, dict[str, object]] = {}
    workers = min(MAX_PARALLEL_REQUESTS, len(chunks))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(review_chunk, api_key, model, chunk, index, len(chunks)): index
            for index, chunk in enumerate(chunks, start=1)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return [results[index] for index in range(1, len(chunks) + 1)]


def _clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def validate_findings(
    responses: list[dict[str, object]],
    chunks: tuple[ReviewChunk, ...],
    review_files: list[ReviewFile],
) -> list[Finding]:
    files_by_path = {review_file.path: review_file for review_file in review_files}
    findings: list[Finding] = []
    seen_locations: set[tuple[str, str, int]] = set()

    for response, chunk in zip(responses, chunks, strict=True):
        raw_findings = response.get("findings", [])
        if not isinstance(raw_findings, list):
            continue
        for raw in raw_findings:
            if not isinstance(raw, dict):
                continue
            severity = raw.get("severity")
            path = raw.get("path")
            side = raw.get("side")
            line = raw.get("line")
            if isinstance(path, str) and path.startswith("./"):
                path = path[2:]
            if (
                severity not in {"P1", "P2"}
                or not isinstance(path, str)
                or path not in chunk.paths
                or path not in files_by_path
                or side not in {"RIGHT", "LEFT"}
                or isinstance(line, bool)
                or not isinstance(line, int)
            ):
                continue
            valid_lines = (
                files_by_path[path].right_lines if side == "RIGHT" else files_by_path[path].left_lines
            )
            location = (path, side, line)
            title = _clean_text(raw.get("title"), 160)
            impact = _clean_text(raw.get("impact"), 700)
            fix = _clean_text(raw.get("fix"), 700)
            if line not in valid_lines or location in seen_locations or not all((title, impact, fix)):
                continue
            seen_locations.add(location)
            findings.append(Finding(severity, path, side, line, title, impact, fix))

    findings.sort(key=lambda finding: (0 if finding.severity == "P1" else 1, finding.path, finding.line))
    return findings[:MAX_FINDINGS]


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _review_body(model: str, head_sha: str, plan: ReviewPlan, findings: list[Finding]) -> str:
    marker = f"<!-- openrouter-pr-review:{head_sha} -->"
    reviewed_count = len(plan.complete_files)
    if plan.complete:
        coverage = f"complete — {reviewed_count}/{plan.total_files} eligible changed files"
    else:
        coverage = (
            f"partial — {reviewed_count}/{plan.total_files} files complete, "
            f"{len(plan.partial_files)} partial, {len(plan.omitted_files)} omitted by the bounded budget"
        )
    summary = (
        f"Reviewed the PR in {len(plan.chunks)} bounded chunk(s); "
        + (f"found {len(findings)} high-confidence issue(s)." if findings else "found no actionable issues.")
    )
    lines = [
        marker,
        "## OpenRouter API review",
        "",
        f"> Provider: OpenRouter · Model: `{model}`",
        f"> Coverage: {coverage}",
        "",
        f"Summary: {summary}",
        "",
    ]
    if findings:
        lines.append("Findings (also attached to the exact changed lines when GitHub accepts them):")
        lines.extend(
            f"- **{finding.severity} — `{finding.path}:{finding.line}`**: {finding.title}. "
            f"{finding.impact} Proposed fix: {finding.fix}"
            for finding in findings
        )
    else:
        lines.append("Findings: No actionable findings.")
    return "\n".join(lines)


def publish_review(
    repository: str,
    pr_number: str,
    token: str,
    model: str,
    head_sha: str,
    plan: ReviewPlan,
    findings: list[Finding],
) -> None:
    headers = _github_headers(token)
    reviews_url = f"{GITHUB_API_URL}/repos/{repository}/pulls/{pr_number}/reviews"
    marker = f"<!-- openrouter-pr-review:{head_sha} -->"
    existing_reviews = request_json(f"{reviews_url}?per_page=100", "GET", headers)
    if not isinstance(existing_reviews, list):
        raise RuntimeError("GitHub returned an invalid pull-request review list")
    if any(marker in (review.get("body") or "") for review in existing_reviews if isinstance(review, dict)):
        print("The OpenRouter formal review already exists for this commit; skipping duplicate.")
        return

    body = _review_body(model, head_sha, plan, findings)
    comments = [
        {
            "path": finding.path,
            "line": finding.line,
            "side": finding.side,
            "body": (
                f"**{finding.severity} — {finding.title}**\n\n"
                f"Impact: {finding.impact}\n\nProposed fix: {finding.fix}"
            ),
        }
        for finding in findings
    ]
    payload: dict[str, object] = {
        "commit_id": head_sha,
        "body": body,
        "event": "COMMENT",
        "comments": comments,
    }
    try:
        request_json(reviews_url, "POST", headers, payload)
    except RuntimeError:
        if not comments:
            raise
        # Preserve the review and its findings even if GitHub rejects one inline anchor.
        request_json(
            reviews_url,
            "POST",
            headers,
            {"commit_id": head_sha, "body": body, "event": "COMMENT"},
        )
        print("Created the OpenRouter formal PR review; GitHub rejected its inline anchors.")
        return
    print(f"Created the OpenRouter formal PR review with {len(comments)} inline finding(s).")


def main() -> None:
    api_key = required_env("OPENROUTER_API_KEY")
    github_token = required_env("GITHUB_TOKEN")
    model = required_env("OPENROUTER_REVIEW_MODEL").strip()
    if not model:
        raise RuntimeError("OPENROUTER_REVIEW_MODEL must not be blank")
    repository = required_env("GITHUB_REPOSITORY")
    pr_number = required_env("PR_NUMBER")
    base_sha = required_env("BASE_SHA")
    head_sha = required_env("HEAD_SHA")

    review_files = read_review_files(base_sha, head_sha)
    plan = build_review_plan(review_files)
    responses = review_chunks(api_key, model, plan.chunks)
    findings = validate_findings(responses, plan.chunks, review_files)
    publish_review(repository, pr_number, github_token, model, head_sha, plan, findings)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"OpenRouter PR review failed: {error}", file=sys.stderr)
        sys.exit(1)

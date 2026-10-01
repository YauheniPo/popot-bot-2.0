#!/usr/bin/env python3
"""Validate an agent-written digest, stage it, and publish rated selections."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

HEADING_PATTERN = re.compile(r"^## (\\d+)\. (.+)$")
SOURCE_AVAILABILITY_HEADER = "\n## Source availability\n"
import tempfile


URL = re.compile(r"https?://[^\s<>\)\]]+")
RUN_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")  # noqa: S6353
MAX_RAW_BYTES = 10_485_760


def _validate_output_dir(output_dir: Path) -> Path:
    """Resolve and validate output directory, rejecting path traversal."""
    if ".." in output_dir.parts:
        raise ValueError("invalid output directory")
    resolved = output_dir.resolve()
    if not resolved.is_absolute():  # pragma: no cover
        raise ValueError("invalid output directory")
    return resolved


def _validate_report(raw: dict, draft: str, items: list[dict], *,
                     citation_items: list[dict] | None = None) -> None:
    """Validate report structure and content against collected items."""
    run_id = raw.get("run_id", "")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run_id")
    if not isinstance(items, list) or not items:
        raise ValueError("report has no source items")
    headings = list(HEADING_PATTERN.finditer(draft))
    if len(headings) != len(items):
        raise ValueError("report item count does not match collected items")
    _validate_citations(draft, citation_items if citation_items is not None else items)
    for index, (heading, item) in enumerate(zip(headings, items), 1):
        section_end = headings[index].start() if index < len(headings) else len(draft)
        _validate_section(heading, draft[heading.end():section_end], item, index)


def _validate_citations(draft: str, items: list[dict]) -> None:
    allowed_urls = {url for item in items for url in item.get("urls", [])}
    cited_urls = {match.group().rstrip(".,;") for match in URL.finditer(draft)}
    if cited_urls - allowed_urls:
        raise ValueError("report cites a URL absent from collected sources")


def _validate_section(heading, section: str, item: dict, index: int) -> None:
    if heading.group(1) != str(index) or heading.group(2).strip() != item["title"]:
        raise ValueError("report title or order differs from collected items")
    roles = re.findall(r"^### (Junior|Senior|Manager)$", section, re.M)
    if roles != ["Junior", "Senior", "Manager"]:
        raise ValueError("report requires Junior, Senior, Manager in that order")
    section_urls = {match.group().rstrip(".,;") for match in URL.finditer(section)}
    if not set(item.get("urls", [])) <= section_urls:
        raise ValueError("report item is missing source URLs")


def _validate_source_availability(draft: str, issues: list[dict]) -> None:
    """Verify all source issues are documented in the availability block."""
    if not issues:
        return
    availability = draft.split(SOURCE_AVAILABILITY_HEADER, 1)
    if len(availability) != 2:
        raise ValueError("report omits unavailable or degraded sources")
    for issue in issues:
        issue_id = issue.get("id")
        if not issue_id:
            raise ValueError("malformed source issue entry: missing 'id'")
        if not re.search(rf"(?<![\w-]){re.escape(issue_id)}(?![\w-])", availability[1]):
            raise ValueError("report omits unavailable or degraded sources")


def complete_missing_analysis(raw: dict, draft: str) -> str:
    """Keep valid item sections and mark missing analysis explicitly."""
    items = raw.get("items", [])
    headings = list(HEADING_PATTERN.finditer(draft))
    sections: dict[tuple[str, str], tuple[re.Match, str]] = {}
    for heading in headings:
        next_heading = re.search(r"^## ", draft[heading.end():], re.M)
        end = heading.end() + next_heading.start() if next_heading else len(draft)
        key = heading.group(1), heading.group(2).strip()
        sections[key] = heading, draft[heading.end():end]

    output = ["# AI/IT News Digest"]
    for index, item in enumerate(items, 1):
        existing = sections.get((str(index), item["title"]))
        if existing:
            heading, body = existing
            try:
                _validate_section(heading, body, item, index)
                _validate_citations(body, items)
            except ValueError:
                existing = None
        if existing:
            output.append(f"## {index}. {item['title']}{body.rstrip()}")
            continue
        urls = item.get("urls") or []
        sources = "\n".join(f"Sources: {url}" for url in urls)
        output.append(f"""## {index}. {item['title']}
{sources}
### Junior
Анализ недоступен. Откройте источник по ссылке в карточке.
### Senior
Анализ недоступен. Проверьте первоисточник перед использованием.
### Manager
Анализ недоступен. Решение по этой новости требует проверки источника.""")
    completed = "\n\n".join(output) + "\n"
    _validate_report(raw, completed, items)
    return completed


def _complete_source_availability(draft: str, issues: list[dict]) -> str:
    """Add missing source IDs from collected metadata to a staged report."""
    if not issues:
        return draft
    marker = SOURCE_AVAILABILITY_HEADER
    if marker not in draft:
        draft = draft.rstrip() + "\n" + marker
    availability = draft.split(marker, 1)[1]
    missing: dict[str, set[str]] = {}
    for issue in issues:
        issue_id = issue.get("id")
        if not isinstance(issue_id, str) or not issue_id:
            raise ValueError("malformed source issue entry: missing 'id'")
        if not re.search(rf"(?<![\w-]){re.escape(issue_id)}(?![\w-])", availability):
            kind = issue.get("kind")
            missing.setdefault(issue_id, set()).add(
                kind if isinstance(kind, str) and kind in {"failed", "empty", "degraded", "unavailable"}
                else "issue")
    if missing:
        lines = [f"- {issue_id}: {'/'.join(sorted(kinds))}" for issue_id, kinds in missing.items()]
        draft = draft.rstrip() + "\n\n" + "\n".join(lines) + "\n"
    return draft


def finalize(raw: dict, draft: str, output_dir: Path) -> Path:
    items = raw.get("items", [])
    _validate_report(raw, draft, items)
    issues = raw.get("source_issues", [])
    _validate_source_availability(draft, issues)
    validated_dir = _validate_output_dir(output_dir)
    validated_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    target = validated_dir / f"digest-{raw['run_id']}.md"
    with tempfile.TemporaryDirectory(prefix=".digest-", dir=validated_dir) as staging_dir:
        staging = Path(staging_dir)
        os.chmod(staging, 0o700)
        temporary = staging / "draft.md"
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            stream.write(draft)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    return target


def stage(raw: dict, draft: str, state_dir: Path) -> Path:
    """Keep the full analysis private until its Telegram cards have been rated."""
    _validate_report(raw, draft, raw.get("items", []))
    draft = _complete_source_availability(draft, raw.get("source_issues", []))
    _validate_source_availability(draft, raw.get("source_issues", []))
    state_dir = _validate_output_dir(state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = state_dir / f"staged-{raw['run_id']}.md"
    with tempfile.TemporaryDirectory(prefix=".staged-", dir=state_dir) as directory:
        temporary = Path(directory) / "draft.md"
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            stream.write(draft)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    return target


def finalize_selected(raw: dict, staged_path: Path, selected_indexes: list[int],
                      output_dir: Path) -> Path:
    """Publish only score-3 items, preserving the validated detailed sections."""
    staged = _validate_state_path(staged_path, staged_path.parent)
    draft = staged.read_text(encoding="utf-8")
    items = raw.get("items", [])
    _validate_report(raw, draft, items)
    _validate_source_availability(draft, raw.get("source_issues", []))
    if selected_indexes != sorted(set(selected_indexes)) or any(
            not isinstance(index, int) or index < 0 or index >= len(items)
            for index in selected_indexes):
        raise ValueError("invalid selected item indexes")
    headings = list(HEADING_PATTERN.finditer(draft))
    availability_start = draft.find(SOURCE_AVAILABILITY_HEADER)
    report_end = availability_start if availability_start >= 0 else len(draft)
    sections = [draft[heading.start():min(
        headings[index + 1].start() if index + 1 < len(headings) else report_end,
        report_end)].strip() for index, heading in enumerate(headings)]
    header = draft[:headings[0].start()].rstrip()
    footer = draft[availability_start:].strip() if availability_start >= 0 else ""
    selected = [re.sub(r"^## \d+\.", f"## {order}.", sections[index], count=1)
                for order, index in enumerate(selected_indexes, 1)]
    if selected:
        content = "\n\n".join([header, *selected, footer]).strip() + "\n"
        _validate_report({"run_id": raw["run_id"]}, content,
                         [items[index] for index in selected_indexes], citation_items=items)
    else:
        content = "\n\n".join([header, "## No news rated 3\n\nВ этом выпуске нет новостей с оценкой 3.", footer]).strip() + "\n"
    _validate_source_availability(content, raw.get("source_issues", []))
    output_dir = _validate_output_dir(output_dir)
    output_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    target = output_dir / f"digest-{raw['run_id']}.md"
    with tempfile.TemporaryDirectory(prefix=".digest-", dir=output_dir) as directory:
        temporary = Path(directory) / "selected.md"
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    return target


def _read_raw(path: Path, state_dir: Path) -> dict:
    path = _validate_state_path(path, state_dir)
    with path.open("rb") as stream:
        payload = stream.read(MAX_RAW_BYTES + 1)
    if len(payload) > MAX_RAW_BYTES:
        raise ValueError("raw file too large")
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("raw file must contain a JSON object")
    return raw


def _validate_state_path(path: Path, state_dir: Path) -> Path:
    state_root = state_dir.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(state_root):
        raise ValueError("input file outside state directory")
    if not resolved.is_file():
        raise ValueError("input path is not a file")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--stage", action="store_true",
                        help="validate and stage analysis for Telegram ratings")
    args = parser.parse_args(argv)
    state_dir = Path(os.environ.get("AI_DIGEST_STATE_DIR", "~/.hermes/ops/news")).expanduser()
    output_dir = Path(os.environ.get("AI_DIGEST_OUTPUT_DIR", "~/workspace/digests")).expanduser()
    draft_path = _validate_state_path(args.draft, state_dir)
    raw = _read_raw(args.raw, state_dir)
    draft = draft_path.read_text(encoding="utf-8")
    print(stage(raw, draft, state_dir) if args.stage else finalize(raw, draft, output_dir))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
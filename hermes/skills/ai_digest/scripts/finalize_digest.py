#!/usr/bin/env python3
"""Validate an agent-written digest and archive it without overwriting a run."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
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


def _validate_report(raw: dict, draft: str, items: list[dict]) -> None:
    """Validate report structure and content against collected items."""
    run_id = raw.get("run_id", "")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run_id")
    if not isinstance(items, list) or not items:
        raise ValueError("report has no source items")
    headings = list(re.finditer(r"^## (\d+)\. (.+)$", draft, re.M))
    if len(headings) != len(items):
        raise ValueError("report item count does not match collected items")
    _validate_citations(draft, items)
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
    availability = draft.split("\n## Source availability\n", 1)
    if len(availability) != 2:
        raise ValueError("report omits unavailable or degraded sources")
    for issue in issues:
        issue_id = issue.get("id")
        if not issue_id:
            raise ValueError("malformed source issue entry: missing 'id'")
        if not re.search(rf"(?<![\w-]){re.escape(issue_id)}(?![\w-])", availability[1]):
            raise ValueError("report omits unavailable or degraded sources")


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


def _read_raw(path: Path) -> dict:
    with path.open("rb") as stream:
        payload = stream.read(MAX_RAW_BYTES + 1)
    if len(payload) > MAX_RAW_BYTES:
        raise ValueError("raw file too large")
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("raw file must contain a JSON object")
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    args = parser.parse_args(argv)
    output_dir = Path(os.environ.get("AI_DIGEST_OUTPUT_DIR", "~/workspace/digests")).expanduser()
    raw = _read_raw(args.raw)
    print(finalize(raw, args.draft.read_text(encoding="utf-8"), output_dir))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

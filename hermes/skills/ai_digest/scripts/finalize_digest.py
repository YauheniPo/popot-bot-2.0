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
RUN_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")


def _validate_output_dir(output_dir: Path) -> Path:
    """Resolve and validate output directory, rejecting path traversal."""
    if ".." in output_dir.parts:
        raise ValueError("invalid output directory")
    resolved = output_dir.resolve()
    if not resolved.is_absolute():
        raise ValueError("invalid output directory")
    return resolved


def _validate_report(raw: dict, draft: str, items: list[dict]) -> None:
    """Validate report structure and content against collected items."""
    run_id = raw.get("run_id", "")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run_id")
    if not isinstance(items, list) or not items:
        raise ValueError("report has no source items")
    headings = list(re.finditer(r"^## ([1-9][0-9]*)\. (.+)$", draft, re.M))
    if len(headings) != len(items):
        raise ValueError("report item count does not match collected items")
    allowed_urls = {url for item in items for url in item.get("urls", [])}
    cited_urls = {match.group().rstrip(".,;") for match in URL.finditer(draft)}
    if cited_urls - allowed_urls:
        raise ValueError("report cites a URL absent from collected sources")
    for index, (heading, item) in enumerate(zip(headings, items), 1):
        if heading.group(1) != str(index) or heading.group(2).strip() != item["title"]:
            raise ValueError("report title or order differs from collected items")
        section = draft[heading.end():headings[index].start() if index < len(headings) else len(draft)]
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
    if len(availability) != 2 or any(
            not re.search(rf"(?<![\w-]){re.escape(issue['id'])}(?![\w-])", availability[1])
            for issue in issues):
        raise ValueError("report omits unavailable or degraded sources")


def finalize(raw: dict, draft: str, output_dir: Path) -> Path:
    items = raw.get("items", [])
    _validate_report(raw, draft, items)
    issues = raw.get("source_issues", [])
    _validate_source_availability(draft, issues)
    validated_dir = _validate_output_dir(output_dir)
    validated_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    target = validated_dir / f"digest-{raw['run_id']}.md"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix=".digest-",
                                     suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.chmod(temporary, 0o640)
            stream.write(draft)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path,
                        default=Path(os.environ.get("AI_DIGEST_OUTPUT_DIR", "~/workspace/digests")).expanduser())
    args = parser.parse_args(argv)
    raw = json.loads(args.raw.read_text(encoding="utf-8"))
    print(finalize(raw, args.draft.read_text(encoding="utf-8"), args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

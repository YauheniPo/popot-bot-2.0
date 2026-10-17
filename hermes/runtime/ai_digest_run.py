"""Bind AI digest publication to validated artifacts from one cron execution."""
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import shlex
import uuid


class AnalysisError(ValueError):
    """A draft can be corrected using the collected sources, without new research."""


class DigestRun:
    def __init__(self, home):
        self.state = Path(os.environ.get("AI_DIGEST_STATE_DIR") or Path(home) / "ops/news").expanduser().resolve()
        skill_dir = Path(os.environ.get("AI_DIGEST_SKILL_DIR", "")).expanduser()
        if not skill_dir.is_absolute() or not (skill_dir / "scripts/finalize_digest.py").is_file():
            raise ValueError("AI digest: managed skill unavailable")
        spec = importlib.util.spec_from_file_location(
            "ai_digest_finalizer", skill_dir / "scripts/finalize_digest.py")
        if spec is None or spec.loader is None:
            raise ValueError("AI digest: managed skill unavailable")
        self.finalizer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.finalizer)
        self.collector = skill_dir / "scripts/collect_news.py"
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        self.raw_path = self.state / f"raw-{self.run_id}.json"
        self.draft_path = self.state / f"draft-{self.run_id}.md"
        self.staged_path = self.state / f"staged-{self.run_id}.md"

    def instructions(self, extra_prompt=None):
        return (str(extra_prompt or "") + "\n\nAI DIGEST RUNTIME PUBLICATION CONTRACT "
                "(overrides earlier collection, filtering and artifact-path instructions): "
                "For this execution run python3 " + shlex.quote(str(self.collector))
                + " --run-id " + self.run_id + " --state-dir " + shlex.quote(str(self.state))
                + " with the requested --mode and limits from sources.json. "
                "The collector already excludes previously delivered stories. Do not add manual "
                "deduplication from previous-run prose, remove items, or create a filtered JSON. "
                "Read the original collection at " + str(self.raw_path)
                + ". Write all selected items and their Russian Junior/Senior/Manager analysis to "
                + str(self.draft_path) + ". Follow ai_digest validation and source attribution rules. "
                "The scheduler validates these exact artifacts and generates the delivery marker; "
                "a final claim that cards are ready or [SILENT] is not a result. "
                "Do not send messages yourself. On collection failure report [CRON_FAILURE] "
                "on its own first line, then the actual error.")

    def _path(self, path):
        if path.is_symlink() or path.resolve().parent != self.state:
            raise ValueError("AI digest: unsafe artifact path")
        if path.exists() and not path.is_file():
            raise ValueError("AI digest: unsafe artifact type")
        return path

    def _raw(self):
        path = self._path(self.raw_path)
        if not path.is_file():
            raise ValueError("AI digest: collection missing for this execution")
        raw = self.finalizer._read_raw(path, self.state)
        items = raw.get("items")
        if raw.get("run_id") != self.run_id:
            raise ValueError("AI digest: collection belongs to another execution")
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValueError("AI digest: no suitable items or invalid collection size")
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get("title"), str)
                    or not item["title"].strip() or not isinstance(item.get("urls"), list)
                    or not item["urls"] or not all(isinstance(url, str) for url in item["urls"])
                    or item.get("url") not in item["urls"]):
                raise ValueError("AI digest: invalid collected item")
        return raw

    def _analysis(self, path):
        self._path(path)
        if not path.is_file():
            raise AnalysisError("AI digest: analysis missing for this execution")
        # Keep file reads bounded just like the raw collection.
        with path.open("rb") as stream:
            data = stream.read(self.finalizer.MAX_RAW_BYTES + 1)
        if len(data) > self.finalizer.MAX_RAW_BYTES:
            raise AnalysisError("AI digest: analysis exceeds size limit")
        try:
            return data.decode("utf-8")
        except UnicodeError:
            raise AnalysisError("AI digest: analysis must be UTF-8") from None

    def finalize(self):
        raw = self._raw()
        self._path(self.staged_path)
        draft = self._analysis(self.staged_path if self.staged_path.exists() else self.draft_path)
        try:
            self.finalizer._validate_report(raw, draft, raw["items"])
            if self.staged_path.exists():
                self.finalizer._validate_source_availability(draft, raw.get("source_issues", []))
            else:
                self.finalizer.stage(raw, draft, self.state)
        except ValueError as error:
            raise AnalysisError(f"AI digest: {error}") from None
        return f"NEWS_CARDS:{self.raw_path}"

    def finalize_with_repair(self, repair, *, allow_research=True):
        # Shared scheduler contract; AI digest corrections never collect new sources.
        try:
            return self.finalize()
        except AnalysisError as error:
            repair("AI digest artifact validation failed: " + str(error)
                   + ". One correction attempt. Read " + str(self.raw_path) + " and correct "
                   + str(self.draft_path) + " (and " + str(self.staged_path)
                   + " if already present) using only collected evidence. Include every original "
                   "item with exact title, sources and Junior/Senior/Manager sections. "
                   "Do not repeat collection, filter or rewrite raw JSON, invent analysis, "
                   "search again, send messages, or use old files. Report a failure if the "
                   "collected evidence cannot support the requested analysis.")
        return self.finalize()

    def commit(self):
        """Card delivery and ratings are recorded by feedback.py, not by this guard."""

    def close(self):
        """No lock or background worker: each execution owns unique artifact paths."""


def begin_run(job, home):
    skills = job.get("skills") or job.get("skill") or []
    if isinstance(skills, str):
        skills = [skills]
    if job.get("no_agent") or "ai_digest" not in skills:
        return None
    if "competitor-news-monitor" in skills:
        raise ValueError("AI digest and competitor monitor require separate cron jobs")
    return DigestRun(home)

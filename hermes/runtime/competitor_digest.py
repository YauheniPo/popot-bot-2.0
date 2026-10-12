"""Managed competitor cron gate: validate, render, then checkpoint delivery.

This checks the artifact contract, not the factual truth of a source's claims.
No direct network access; the scheduler supplies the bounded repair callback.
Each watch has a nonblocking process lock.
"""
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

MAX_JSON_BYTES = 2 * 1024 * 1024
STATE_FIELDS = {"last_cutoff", "last_success_at", "last_run_record", "last_run_at", "last_run_status"}
SKILL = "competitor-news-monitor"


class ReportValidationError(ValueError):
    """Repairable artifact content error; never a path or ownership failure."""


class ReportIdentityError(ValueError):
    """Explicit run/window mismatch that must not be repaired or overwritten."""


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("competitor report: invalid UTC timestamp") from None


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError("competitor report: missing or oversized text field")
    return value.strip()


def url(value):
    value = text(value)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or any(c.isspace() for c in value)
                or any(marker in value for marker in ("NEWS_CARDS:", "MEDIA:", "[["))):
            raise ValueError
        # Accessing port validates malformed ports without resolving/fetching the URL.
        parsed.port
    except ValueError:
        raise ValueError("competitor report: invalid evidence URL") from None
    return value


def canonical_url(value):
    parsed = urlsplit(url(value))
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                       parsed.path.rstrip("/"), urlencode(sorted(query)), ""))


def secure_path(root, path):
    """Reject lexical traversal and symlinks, including intermediate directories."""
    root, path = Path(root), Path(path)
    if ".." in path.parts or not path.is_relative_to(root):
        raise ValueError("competitor report: path outside watch directory")
    current = root
    for part in ("", *path.relative_to(root).parts):
        current = current / part
        if current.is_symlink():
            raise ValueError("competitor report: symlink path refused")
    return path


def read_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("competitor report: JSON must be a regular file")
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError("competitor report: JSON exceeds size limit")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("competitor report: expected JSON object")
        return result
    except (OSError, json.JSONDecodeError, UnicodeError):
        raise ValueError("competitor report: JSON missing or unreadable") from None


def write_json(path, value):
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode()
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("competitor report: JSON exceeds size limit")
    fd, temporary = tempfile.mkstemp(prefix=".competitor-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def config(watch):
    return {k: v for k, v in watch.items() if k not in STATE_FIELDS}


def strings(value):
    if not isinstance(value, list):
        raise ValueError("competitor report: expected list")
    return [text(item) for item in value]


def event_keys(event):
    # Syndicated URLs still need the model's semantic deduplication. Shared
    # evidence URLs on the same company/category/date are deterministic duplicates.
    return {hashlib.sha256(json.dumps([event["company"], event["category"],
            event["event_date"][:10], canonical_url(link)]).encode()).hexdigest()
            for link in event["primary_evidence_urls"]}


def _validate_row(row, companies, covered):
    if not isinstance(row, dict) or row.get("company") not in companies - covered:
        raise ValueError("competitor report: unknown or duplicate coverage company")
    new_covered = covered | {row["company"]}
    checked = strings(row.get("checked_urls"))
    for link in checked:
        url(link)
    gaps = strings(row.get("gaps"))
    status = row.get("status")
    if (status not in {"complete", "partial", "not_checked"}
            or status == "complete" and (not checked or gaps)
            or status != "complete" and not gaps
            or status == "not_checked" and checked):
        raise ValueError("competitor report: inconsistent coverage status")
    return new_covered


def validate_coverage(coverage, companies):
    if not isinstance(coverage, list) or len(coverage) != len(companies):
        raise ValueError("competitor report: missing company coverage")
    covered = set()
    for row in coverage:
        covered = _validate_row(row, companies, covered)
    return coverage

def _validate_event_fields(event):
    for field in ("what_changed", "why_it_matters", "confidence", "confidence_reason"):
        text(event.get(field))


def _check_event_links(event, coverage):
    links = strings(event.get("primary_evidence_urls"))
    if not links:
        raise ValueError("competitor report: missing evidence URLs")
    checked = next(c["checked_urls"] for c in coverage if c["company"] == event["company"])
    if not {canonical_url(link) for link in links} <= {canonical_url(link) for link in checked}:
        raise ValueError("competitor report: event evidence was not recorded as read")


def validate_event(event, watch, coverage, start, end):
    companies = {c["name"] for c in watch["companies"]}
    if (not isinstance(event, dict) or event.get("company") not in companies
            or event.get("category") not in watch["event_categories"]):
        raise ValueError("competitor report: invalid event company/category")
    value = text(event.get("event_date"))
    try:
        in_window = (start.date() <= date.fromisoformat(value) <= end.date()
                     if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                     else start <= timestamp(value) <= end)
    except ValueError:
        raise ValueError("competitor report: invalid event date")
    if not in_window:
        raise ValueError("competitor report: event outside the requested window")
    _check_event_links(event, coverage)
    _validate_event_fields(event)
    if event.get("follow_up"):
        text(event["follow_up"])

def validate(record, watch, start, end, run_id):
    for field, expected in (("run_id", run_id), ("watch_slug", watch["watch_slug"]),
                            ("window_start", start), ("window_end", end)):
        actual = record.get(field)
        if field.startswith("window_"):
            try:
                actual = timestamp(actual)
            except ValueError:
                actual = None
        if actual != expected:
            raise ReportIdentityError(f"competitor report: conflicting {field}")
    coverage = validate_coverage(record.get("coverage"), {c["name"] for c in watch["companies"]})
    complete = record.get("coverage_complete")
    errors = strings(record.get("errors"))
    if (type(complete) is not bool
            or complete and (errors or any(c["status"] != "complete" for c in coverage))):
        raise ValueError("competitor report: inconsistent coverage_complete")
    if record.get("greeting") is not None and len(text(record["greeting"])) > 160:
        raise ValueError("competitor report: greeting too long")
    events = record.get("events")
    if not isinstance(events, list):
        raise ValueError("competitor report: missing events list")
    seen = set()
    for event in events:
        validate_event(event, watch, coverage, start, end)
        keys = event_keys(event)
        if seen & keys:
            raise ValueError("competitor report: duplicate event")
        seen.update(keys)
    return record


def prose(value):
    # Prevent Markdown/control tokens in model text from creating hidden links,
    # media attachments or scheduler directives. Evidence URLs are rendered separately.
    value = re.sub(r"https?://\S+", "[see source]", text(value))
    return re.sub(r"[\[\]<>*_`#]", "", " ".join(value.split())).replace("MEDIA:", "MEDIA -")


def render(watch, record, events, bootstrap):
    if record["coverage_complete"] and not events:
        return "[SILENT]"
    counts = {status: sum(c["status"] == status for c in record["coverage"])
              for status in ("complete", "partial", "not_checked")}
    title = record.get("greeting") or f"{watch['name']} — competitor brief"
    lines = [f"🌿 {prose(title)}",
             f"Window (UTC): {record['window_start']} — {record['window_end']}",
             "Initial coverage window" if bootstrap else "Update since verified coverage",
             f"{len(events)} verified new event(s) · Coverage: "
             + ("complete" if record["coverage_complete"] else "partial"),
             f"Companies: {counts['complete']} complete · {counts['partial']} partial · "
             f"{counts['not_checked']} not checked", ""]
    if not events:
        lines.append("No verified new events found in the checked sources.")
    for event in events:
        lines += [f"{prose(event['company'])} · {prose(event['category'].replace('_', ' '))} · {event['event_date']}",
                  f"What changed: {prose(event['what_changed'])}",
                  f"Why it matters (Interpretation): {prose(event['why_it_matters'])}",
                  f"Confidence: {prose(event['confidence'])} — {prose(event['confidence_reason'])}"]
        if event.get("follow_up"):
            lines.append(f"Follow-up: {prose(event['follow_up'])}")
        lines += ["Source: " + link for link in event["primary_evidence_urls"]] + [""]
    lines.append("Coverage")
    for row in record["coverage"]:
        lines.append(f"• {prose(row['company'])}: {row['status']}"
                     + (" — " + "; ".join(prose(gap) for gap in row["gaps"]) if row["gaps"] else ""))
    if record["errors"]:
        lines += ["Research limitations: " + "; ".join(prose(error) for error in record["errors"])]
    return "\n".join(lines)


def validate_watch(watch, slug):
    if (watch.get("watch_slug") != slug or watch.get("language") != "en"
            or not isinstance(watch.get("companies"), list)
            or not watch["companies"]):
        raise ValueError("competitor report: unsupported watch contract (requires English)")
    names = [text(c.get("name")) for c in watch["companies"] if isinstance(c, dict)]
    if len(set(names)) != len(watch["companies"]):
        raise ValueError("competitor report: invalid company list")
    strings(watch.get("event_categories"))
    text(watch.get("name"))
    days = watch.get("lookback_days")
    if type(days) is not int or not 1 <= days <= 366:
        raise ValueError("competitor report: invalid lookback_days")
    return days


class RunGuard:
    def __init__(self, home, watch_path):
        self.root = home
        self.watch_path = watch_path
        self.closed = self.committed = False
        self.record = None
        self.lock_fd = None
        try:
            slug = watch_path.stem
            self.directory = secure_path(home, watch_path.parent / slug)
            self.directory.mkdir(mode=0o700, exist_ok=True)
            lock_path = secure_path(home, self.directory / ".publication.lock")
            self.lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("competitor report: watch already running") from None
            # Snapshot only after taking ownership, so an overlapping completed
            # run cannot be rolled back by a stale public-watch snapshot.
            self.watch = read_json(secure_path(home, watch_path))
            days = validate_watch(self.watch, slug)
            self.state_path = secure_path(home, self.directory / ".publication.json")
            self.state = read_json(self.state_path) if self.state_path.exists() else {}
            if self.state and self.state.get("config") != config(self.watch):
                # Changed companies/categories need a fresh coverage window;
                # already delivered evidence remains deduplicated.
                self.state.pop("last_cutoff", None)
                self.state.pop("pending_window_start", None)
            self.window_end = utc_now()
            cutoff = self.state.get("last_cutoff")
            self.bootstrap = not cutoff
            self.window_start = (timestamp(cutoff) - timedelta(days=1) if cutoff
                                 else self.window_end - timedelta(days=days))
            if self.state.get("pending_window_start"):
                self.window_start = min(self.window_start, timestamp(self.state["pending_window_start"]))
            if self.window_start > self.window_end:
                raise ValueError("competitor report: checkpoint is in the future")
            self.run_id = uuid.uuid4().hex
            runs = secure_path(home, self.directory / "runs")
            runs.mkdir(mode=0o700, exist_ok=True)
            self.artifact_path = runs / (self.run_id + ".json")
        except BaseException:
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None
            raise

    def instructions(self, extra_prompt=None):
        return (str(extra_prompt or "") + "\n\nMANDATORY RUNTIME PUBLICATION CONTRACT (overrides "
                "earlier persistence/window instructions): Do not modify the watch JSON or publication "
                "state. Write only the report artifact at " + str(self.artifact_path) + ". "
                "The scheduler owns run_id, watch_slug, window_start/window_end and run timestamps; "
                "omit these fields from your JSON. Research only this UTC window: window_start="
                + self.window_start.isoformat()
                + ", window_end=" + self.window_end.isoformat() + ". "
                "JSON: coverage_complete (boolean), errors (list of strings), coverage (list), events (list); "
                "optional greeting (plain text, max 160 characters, preserve the job's requested "
                "personal salutation without repeating the leaf emoji). Coverage must list "
                "every watch company exactly once with company, status complete/partial/not_checked, "
                "checked_urls (only actually read URLs), gaps (list; required when not complete). "
                "Every event: company, category, event_date, primary_evidence_urls included in its "
                "checked_urls, what_changed, why_it_matters, confidence (string), confidence_reason (string), optional "
                "follow_up. No placeholders. The scheduler validates dates, links, coverage and "
                "duplicates, renders English output, and updates state after confirmed delivery. "
                "Do not discard events merely because they occur in an uncommitted run artifact; "
                "the scheduler deduplicates against its confirmed delivery ledger. "
                "Your final prose or [SILENT] cannot bypass validation.")

    def finalize_with_repair(self, repair):
        """At most one content-repair turn; the caller retains its watchdog and cancellation."""
        try:
            return self.finalize()
        except ReportValidationError as error:
            self.current_watch()
            repair(self.instructions() + "\nValidation failed: " + str(error) + ". "
                   "One correction attempt remains. Read the artifact, then fix its JSON using "
                   "evidence already collected in this conversation. Do not restart research, "
                   "send messages or modify the watch. Never invent URLs, coverage or events to "
                   "pass validation; list unchecked companies as not_checked with explicit gaps, "
                   "and keep coverage_complete=false when research is incomplete.")
        return self.finalize()

    def current_watch(self):
        current = read_json(secure_path(self.root, self.watch_path))
        if config(current) != config(self.watch):
            raise ValueError("competitor report: watch changed during run")
        return current

    def finalize(self):
        self.record = None
        self.current_watch()
        path = secure_path(self.root, self.artifact_path)
        try:
            record = read_json(path)
        except ValueError as error:
            raise ReportValidationError(str(error)) from None
        # The unguessable per-fire artifact path binds the payload to this run.
        # Missing metadata is normal; explicit stale metadata still fails closed.
        for field, value in (("run_id", self.run_id), ("watch_slug", self.watch["watch_slug"]),
                             ("window_start", self.window_start.isoformat()),
                             ("window_end", self.window_end.isoformat())):
            record.setdefault(field, value)
        try:
            validate(record, self.watch, self.window_start, self.window_end, self.run_id)
        except ReportIdentityError:
            raise
        except ValueError as error:
            raise ReportValidationError(str(error)) from None
        except TypeError:
            raise ReportValidationError("competitor report: invalid JSON field type") from None
        prior_keys = set(self.state.get("delivered_event_keys", []))
        events = [event for event in record["events"] if not event_keys(event) & prior_keys]
        record["started_at"] = self.window_end.isoformat()
        record["finished_at"] = utc_now().isoformat()
        write_json(secure_path(self.root, self.artifact_path), record)
        # This in-memory snapshot is authoritative even if the model later edits the file.
        self.record, self.events = record, events
        return render(self.watch, record, events, self.bootstrap)

    def commit(self):
        if self.committed:
            return
        if self.record is None:
            raise ValueError("competitor report: validation required before commit")
        current = self.current_watch()
        state = dict(self.state, config=config(self.watch))
        keys = set(state.get("delivered_event_keys", []))
        for event in self.events:
            keys.update(event_keys(event))
        state["delivered_event_keys"] = sorted(keys)
        if self.record["coverage_complete"]:
            state["last_cutoff"] = self.window_end.isoformat()
            state.pop("pending_window_start", None)
        else:
            state["pending_window_start"] = self.window_start.isoformat()
        state["last_run_record"] = str(self.artifact_path)
        state["last_run_at"] = self.record["finished_at"]
        state["last_run_status"] = "ok" if self.record["coverage_complete"] else "coverage_incomplete"
        write_json(secure_path(self.root, self.artifact_path), self.record)
        # Save the public watch first; the private checkpoint is authoritative on
        # subsequent runs. A failed write never advances the trusted checkpoint.
        for field in STATE_FIELDS:
            current.pop(field, None)
            if field in self.watch:
                current[field] = self.watch[field]
        for field in ("last_run_record", "last_run_at", "last_run_status"):
            current[field] = state[field]
        if self.record["coverage_complete"]:
            current.update(last_cutoff=state["last_cutoff"], last_success_at=state["last_run_at"])
        write_json(secure_path(self.root, self.watch_path), current)
        write_json(secure_path(self.root, self.state_path), state)
        self.committed = True

    def close(self):
        if self.closed:
            return
        try:
            if not self.committed:
                # Undo model-written run metadata, preserving concurrent personal
                # settings. The authoritative checkpoint never trusts these fields.
                current = read_json(secure_path(self.root, self.watch_path))
                restored = {k: v for k, v in current.items() if k not in STATE_FIELDS}
                restored.update({k: v for k, v in self.watch.items() if k in STATE_FIELDS})
                if restored != current:
                    write_json(secure_path(self.root, self.watch_path), restored)
        finally:
            self.closed = True
            os.close(self.lock_fd)


def begin_run(job, home):
    skills = job.get("skills", job.get("skill", []))
    if isinstance(skills, str):
        skills = [skills]
    if job.get("no_agent") or SKILL not in (skills or []):
        return None
    home = Path(home).resolve()
    prefix = r"(?:~/\.hermes/|" + re.escape(str(home) + "/") + r")competitor-watches/"
    slugs = set(re.findall(prefix + r"([a-zA-Z0-9][a-zA-Z0-9_-]*)\.json\b", job.get("prompt", "")))
    if len(slugs) != 1:
        raise ValueError("competitor report: exactly one watch JSON path is required")
    watch_path = secure_path(home, home / "competitor-watches" / (slugs.pop() + ".json"))
    return RunGuard(home, watch_path)

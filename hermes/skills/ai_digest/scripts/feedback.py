"""Private Telegram feedback and deterministic AI digest card delivery."""

from __future__ import annotations

from contextlib import contextmanager
import argparse
import secrets
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


GET_CARD_SQL = "SELECT * FROM cards WHERE card_id=?"
_MARKER = re.compile(r"^NEWS_CARDS:(.+)$", re.M)
_WORDS = re.compile(r"[\w]{3,}", re.UNICODE)
_RUN_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{8}")


def _finalizer():
    spec = importlib.util.spec_from_file_location(
        "ai_digest_finalizer", Path(__file__).with_name("finalize_digest.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("digest finalizer unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _item_id(item: dict) -> str:
    return hashlib.sha256(item["url"].encode("utf-8")).hexdigest()


class FeedbackStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        metadata = self.path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ValueError("feedback database must be an owner-controlled regular file")
        self.path.chmod(0o600)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cards (
                    card_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, item_id TEXT NOT NULL,
                    item_index INTEGER NOT NULL, owner_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL, message_id INTEGER, status TEXT NOT NULL,
                    title TEXT NOT NULL, url TEXT NOT NULL, category TEXT NOT NULL,
                    subcategory TEXT NOT NULL, source_ids TEXT NOT NULL, evidence TEXT NOT NULL,
                    sent_at TEXT,
                    UNIQUE (run_id, item_id, chat_id, thread_id, owner_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS cards_message
                    ON cards (chat_id, message_id) WHERE message_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS ratings (
                    user_id TEXT NOT NULL, item_id TEXT NOT NULL, score INTEGER NOT NULL
                        CHECK (score BETWEEN 1 AND 3), updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, item_id)
                );
                CREATE TABLE IF NOT EXISTS card_votes (
                    card_id TEXT PRIMARY KEY REFERENCES cards(card_id),
                    score INTEGER NOT NULL CHECK (score BETWEEN 1 AND 3),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS completions (
                    run_id TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, path TEXT NOT NULL, message_id INTEGER,
                    PRIMARY KEY (run_id, chat_id, thread_id, owner_id)
                );
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve_card(self, run_id: str, item: dict, chat_id: str, thread_id: str,
                     owner_id: str, item_index: int = 0) -> tuple[str, bool]:
        card_id = secrets.token_hex(8)
        item_id = _item_id(item)
        with self._connect() as db:
            inserted = db.execute("""
                INSERT OR IGNORE INTO cards
                    (card_id, run_id, item_id, item_index, owner_id, chat_id, thread_id,
                     status, title, url, category, subcategory, source_ids, evidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
            """, (card_id, run_id, item_id, item_index, str(owner_id), str(chat_id),
                  str(thread_id or ""), item["title"], item["url"],
                  item.get("category") or "", item.get("subcategory") or "",
                  json.dumps(item.get("source_ids", [])), item.get("evidence") or ""))
            if inserted.rowcount:
                return card_id, True
            row = db.execute("""SELECT card_id FROM cards WHERE run_id=? AND item_id=?
                AND chat_id=? AND thread_id=? AND owner_id=?""",
                (run_id, item_id, str(chat_id), str(thread_id or ""), str(owner_id))).fetchone()
            return row["card_id"], False

    def mark_sent(self, card_id: str, message_id: int) -> None:
        with self._connect() as db:
            db.execute("UPDATE cards SET message_id=?, status='sent', sent_at=? WHERE card_id=?",
                       (message_id, _utc_now(), card_id))

    def card_for_message(self, chat_id: str, message_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cards WHERE chat_id=? AND message_id=?",
                             (str(chat_id), message_id)).fetchone()
            return dict(row) if row else None

    def card(self, card_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(GET_CARD_SQL, (card_id,)).fetchone()
            return dict(row) if row else None

    def rate(self, card_id: str, chat_id: str, message_id: int, user_id: str,
             score: int) -> int:
        if score not in (1, 2, 3):
            raise ValueError("invalid rating")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(GET_CARD_SQL, (card_id,)).fetchone()
            if not row or row["status"] != "sent" or row["chat_id"] != str(chat_id) or row["message_id"] != message_id:
                raise ValueError("unknown digest card")
            if row["owner_id"] != str(user_id):
                raise PermissionError("this digest belongs to another user")
            if db.execute("""SELECT 1 FROM completions WHERE run_id=? AND chat_id=?
                    AND thread_id=? AND owner_id=?""", (row["run_id"], row["chat_id"],
                    row["thread_id"], row["owner_id"])).fetchone():
                raise ValueError("digest already finalized")
            db.execute("""INSERT INTO card_votes (card_id, score, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(card_id) DO UPDATE SET score=excluded.score,
                updated_at=excluded.updated_at""", (card_id, score, _utc_now()))
            db.execute("""INSERT INTO ratings (user_id, item_id, score, updated_at)
                VALUES (?, ?, ?, ?) ON CONFLICT(user_id, item_id) DO UPDATE SET
                score=excluded.score, updated_at=excluded.updated_at""",
                (str(user_id), row["item_id"], score, _utc_now()))
        return score

    def history(self, user_id: str, limit: int = 300) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("""SELECT r.score, r.updated_at, c.title, c.url,
                    c.category, c.subcategory, c.source_ids, c.evidence
                FROM ratings r JOIN cards c ON c.item_id=r.item_id AND c.owner_id=r.user_id
                WHERE r.user_id=? GROUP BY r.item_id ORDER BY r.updated_at DESC LIMIT ?""",
                (str(user_id), limit)).fetchall()
        return [{**dict(row), "source_ids": json.loads(row["source_ids"])} for row in rows]

    def recently_sent_items(self, user_id: str, since: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("""SELECT title, url FROM cards
                WHERE owner_id=? AND status='sent' AND sent_at>=?
                ORDER BY sent_at DESC LIMIT 3000""", (str(user_id), since)).fetchall()
        return [dict(row) for row in rows]

    def stats(self, user_id: str) -> dict:
        with self._connect() as db:
            rows = db.execute("""SELECT score, COUNT(*) AS total FROM ratings
                WHERE user_id=? GROUP BY score""", (str(user_id),)).fetchall()
            delivered = db.execute("""SELECT COUNT(*) FROM cards WHERE owner_id=?
                AND status='sent'""", (str(user_id),)).fetchone()[0]
        return {"rated_items": sum(row["total"] for row in rows),
                "delivered_cards": delivered,
                "ratings": {str(score): next((row["total"] for row in rows
                                               if row["score"] == score), 0)
                            for score in (1, 2, 3)}}

    def mark_completion_sent(self, run_id: str, chat_id: str, thread_id: str,
                             owner_id: str, message_id: int) -> None:
        with self._connect() as db:
            db.execute("""UPDATE completions SET message_id=? WHERE run_id=? AND chat_id=?
                AND thread_id=? AND owner_id=?""",
                (message_id, run_id, str(chat_id), str(thread_id or ""), str(owner_id)))


def preference_bonus(item: dict, history: list[dict]) -> float:
    """Bounded, smoothed topic/source similarity from explicit ratings."""
    if not history:
        return 0.0
    item_words = set(_WORDS.findall((item["title"] + " " + item.get("evidence", "")).lower()))
    total = 0.0
    weight_sum = 2.0  # prior: a few clicks must not determine the whole digest
    for past in history:
        if past["url"] == item["url"]:
            continue
        words = set(_WORDS.findall((past["title"] + " " + past.get("evidence", "")).lower()))
        overlap = len(item_words & words) / max(1, min(len(item_words), len(words)))
        similarity = (0.35 * (item.get("category") == past.get("category") and bool(item.get("category")))
                      + 0.25 * (item.get("subcategory") == past.get("subcategory") and bool(item.get("subcategory")))
                      + 0.20 * bool(set(item.get("source_ids", [])) & set(past.get("source_ids", [])))
                      + 0.20 * overlap)
        if similarity < 0.15:
            continue
        try:
            days = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(past["updated_at"])).days)
        except (KeyError, ValueError, TypeError):
            days = 0
        weight = similarity * math.exp(-days / 90)
        total += weight * (past["score"] - 2)
        weight_sum += weight
    return max(-12.0, min(12.0, 12.0 * total / weight_sum))


def extract_cards_marker(content: str, job: dict, state_dir: Path) -> tuple[str, Path | None]:
    """Remove internal marker from delivery; accept only a direct raw file in state."""
    matches = list(_MARKER.finditer(content))
    clean = _MARKER.sub("", content).strip()
    is_valid_match = len(matches) == 1 and "ai_digest" in ([job.get("skill")] + (job.get("skills") or []))
    if not is_valid_match:
        return clean, None
    candidate = Path(matches[0].group(1).strip())
    if not candidate.is_absolute() or not _RUN_ID.fullmatch(candidate.stem.removeprefix("raw-")):
        return clean, None
    if candidate.resolve().parent != state_dir.resolve() or not candidate.is_file():
        return clean, None
    try:
        raw = _finalizer()._read_raw(candidate, state_dir)
    except (OSError, ValueError):
        return clean, candidate.resolve()
    items = raw.get("items")
    if isinstance(items, list) and 1 <= len(items) <= 20:
        count = len(items)
        if count == 1:
            noun = "новость"
        elif count <= 4:
            noun = "новости"
        else:
            noun = "новостей"
        clean = f"Собрано {count} {noun}. Оцените каждую Telegram-карточку от 1 до 3."
    return clean, candidate.resolve()


def _card_text(item: dict, index: int, total: int, analysis: str = "") -> str:
    category = item.get("subcategory") or item.get("category") or "AI/IT"
    description = re.sub(r"\s+", " ", analysis).strip()[:650]
    if (_finalizer().is_empty_analysis(description) or "Анализ недоступен" in description
            or not re.search(r"[А-Яа-яЁё]", description)):
        evidence = re.sub(r"\s+", " ", item.get("evidence") or "").strip()[:550]
        if not evidence:
            raise ValueError(f"source description unavailable for item {index}")
        scope = {"abstract": "аннотации", "repository_readme": "README проекта",
                 "page_text": "страницы"}.get(item.get("evidence_kind"), "источника")
        description = f"По тексту {scope}: {evidence} Анализ недоступен."
    return f"{index}/{total} · {item['title']}\n{category}\n\n{description}\n\n{item['url']}\n\nОцени релевантность: 1–3"


def _keyboard(card_id: str) -> dict:
    return {"inline_keyboard": [[{"text": str(score), "callback_data": f"nd:{card_id}:{score}"}
                                 for score in (1, 2, 3)]]}


def telegram_send(token: str, method: str, payload: dict, *, timeout: int = 15) -> dict:
    request = Request(f"https://api.telegram.org/bot{token}/{method}",
                      data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.load(response)
            break
        except HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                try:
                    wait = int(exc.headers.get("Retry-After", "1"))
                except (ValueError, TypeError):
                    wait = 1
                time.sleep(max(1, min(wait, 10)))
                continue
            # HTTPError URLs include the bot token. Never propagate the original.
            raise RuntimeError(f"Telegram {method} failed (HTTP {exc.code})") from None
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"Telegram {method} failed ({type(exc).__name__})") from None
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} returned an error")
    return result["result"]


def _stage_cards(finalizer, raw: dict, items: list[dict], state_dir: Path,
                 run_id: str) -> Path:
    staged_path = Path(state_dir) / f"staged-{run_id}.md"
    if not staged_path.is_file():
        draft_path = Path(state_dir) / f"draft-{run_id}.md"
        draft = (finalizer._validate_state_path(draft_path, Path(state_dir)).read_text(encoding="utf-8")
                 if draft_path.is_file() else "")
        try:
            finalizer._validate_report(raw, draft, items)
        except ValueError:
            draft = finalizer.complete_missing_analysis(raw, draft)
        try:
            finalizer.stage(raw, draft, Path(state_dir))
        except FileExistsError:
            if not staged_path.is_file():
                raise
    return staged_path


def _card_texts(staged_path: Path, items: list[dict]) -> list[str]:
    staged_text = staged_path.read_text(encoding="utf-8")
    sections = re.split(r"^## \d+\. .+$", staged_text, flags=re.M)[1:]
    card_texts = []
    for index, item in enumerate(items):
        analysis = ""
        if index < len(sections):
            match = re.search(r"^### Junior[ \t]*\n(.*)(?=^### Senior[ \t]*$)",
                              sections[index], flags=re.M | re.S)
            analysis = match.group(1).strip() if match else ""
        card_texts.append(_card_text(item, index + 1, len(items), analysis))
    return card_texts


def deliver_cards(raw_path: Path, state_dir: Path, target: dict, owner_id: str, send) -> int:
    """Send one silent Telegram card per item, without retrying uncertain sends."""
    finalizer = _finalizer()
    raw = finalizer._read_raw(Path(raw_path), Path(state_dir))
    run_id = raw.get("run_id", "")
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid digest run")
    items = raw.get("items")
    if not isinstance(items, list) or not items or len(items) > 20:
        raise ValueError("invalid digest card count")
    staged_path = _stage_cards(finalizer, raw, items, Path(state_dir), run_id)
    store = FeedbackStore(Path(state_dir) / "feedback.db")
    card_texts = _card_texts(staged_path, items)
    count = 0
    for index, item in enumerate(items):
        card_id, fresh = store.reserve_card(run_id, item, target["chat_id"],
                                            target.get("thread_id") or "", owner_id, index)
        if not fresh:
            continue
        if count:
            time.sleep(1)
        message_id = send(target["chat_id"], target.get("thread_id"),
                          card_texts[index], _keyboard(card_id))
        store.mark_sent(card_id, int(message_id))
        count += 1
    return count


def deliver_cards_to_telegram(raw_path: Path, state_dir: Path, target: dict,
                              owner_id: str, token: str) -> int:
    if not token:
        raise RuntimeError("Telegram bot token unavailable")

    def send(chat_id, thread_id, text, keyboard):
        payload = {"chat_id": chat_id, "text": text, "reply_markup": keyboard,
                   "disable_notification": True, "link_preview_options": {"is_disabled": True}}
        if thread_id is not None:
            payload["message_thread_id"] = int(thread_id)
        return telegram_send(token, "sendMessage", payload)["message_id"]

    return deliver_cards(raw_path, state_dir, target, owner_id, send)


def complete_if_ready(store: FeedbackStore, card_id: str, state_dir: Path,
                      output_dir: Path) -> Path | None:
    """Create the score-3 report once all cards from this run have fresh votes."""
    finalizer = _finalizer()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        card = db.execute(GET_CARD_SQL, (card_id,)).fetchone()
        if not card:
            raise ValueError("unknown digest card")
        run_id = card["run_id"]
        raw = finalizer._read_raw(Path(state_dir) / f"raw-{run_id}.json", Path(state_dir))
        rows = db.execute("""SELECT c.item_index, v.score FROM cards c
            LEFT JOIN card_votes v ON v.card_id=c.card_id
            WHERE c.run_id=? AND c.chat_id=? AND c.thread_id=? AND c.owner_id=?
            AND c.status='sent' ORDER BY c.item_index""",
            (run_id, card["chat_id"], card["thread_id"], card["owner_id"])).fetchall()
        if len(rows) != len(raw["items"]) or any(row["score"] is None for row in rows):
            return None
        if db.execute("""SELECT 1 FROM completions WHERE run_id=? AND chat_id=?
                AND thread_id=? AND owner_id=?""", (run_id, card["chat_id"],
                card["thread_id"], card["owner_id"])).fetchone():
            return None
        selected = [row["item_index"] for row in rows if row["score"] == 3]
        expected = Path(output_dir) / f"digest-{run_id}.md"
        if not expected.exists():
            finalizer.finalize_selected(raw, Path(state_dir) / f"staged-{run_id}.md",
                                        selected, Path(output_dir))
        db.execute("""INSERT INTO completions
            (run_id, chat_id, thread_id, owner_id, path) VALUES (?, ?, ?, ?, ?)""",
            (run_id, card["chat_id"], card["thread_id"], card["owner_id"], str(expected)))
        return expected


def owner_for_digest_jobs(jobs_path: Path) -> str | None:
    """Auto-select a profile only when digest jobs have one distinct owner."""
    try:
        jobs = json.loads(jobs_path.read_text(encoding="utf-8")).get("jobs", [])
    except (OSError, ValueError, AttributeError):
        return None
    owners = {str(job.get("origin", {}).get("user_id")) for job in jobs
              if isinstance(job, dict) and "ai_digest" in ([job.get("skill")] + (job.get("skills") or []))
              and job.get("enabled", True)
              and isinstance(job.get("origin"), dict) and job["origin"].get("user_id")}
    return next(iter(owners)) if len(owners) == 1 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stats", action="store_true", required=True)
    parser.add_argument("--user-id")
    args = parser.parse_args(argv)
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    state = Path(os.environ.get("AI_DIGEST_STATE_DIR", home / "ops" / "news")).expanduser()
    owner = args.user_id or owner_for_digest_jobs(home / "cron" / "jobs.json")
    if not owner:
        print(json.dumps({"error": "digest owner is ambiguous"}))
        return 2
    print(json.dumps(FeedbackStore(state / "feedback.db").stats(owner), ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

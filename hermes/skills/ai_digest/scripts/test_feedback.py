"""Ratings and personalized selection for Telegram news cards."""

import json
import io
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).parent))

from feedback import (FeedbackStore, complete_if_ready, deliver_cards,
                      deliver_cards_to_telegram,
                      extract_cards_marker, owner_for_digest_jobs,
                      preference_bonus, telegram_send)  # noqa: E402


class FeedbackTests(unittest.TestCase):
    RUN_ID = "20260928-090000-abcdef12"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.store = FeedbackStore(self.state / "feedback.db")
        self.item = {"title": "New agent toolkit", "url": "https://example.org/tool",
                     "source_ids": ["github-trending"], "category": "project",
                     "subcategory": "Agents", "evidence": "A coding agent toolkit"}

    def test_rating_is_bound_to_owner_message_and_can_be_changed(self):
        card_id, fresh = self.store.reserve_card(self.RUN_ID, self.item, "-1001", "42", "9")
        self.assertTrue(fresh)
        self.store.mark_sent(card_id, 100)
        with self.assertRaises(PermissionError):
            self.store.rate(card_id, "-1001", 100, "10", 3)
        with self.assertRaises(ValueError):
            self.store.rate(card_id, "-1001", 101, "9", 3)
        self.assertEqual(self.store.rate(card_id, "-1001", 100, "9", 3), 3)
        self.assertEqual(self.store.rate(card_id, "-1001", 100, "9", 1), 1)
        self.assertEqual(len(self.store.history("9")), 1)
        self.assertEqual(self.store.history("9")[0]["score"], 1)
        self.assertEqual(self.store.history("10"), [])
        self.assertEqual(self.store.stats("9")["ratings"], {"1": 1, "2": 0, "3": 0})

    def test_store_refuses_symlinked_database_and_reservation_is_idempotent(self):
        other = self.state / "other.db"
        other.write_text("do not overwrite")
        alias = self.state / "alias.db"
        alias.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "owner-controlled regular file"):
            FeedbackStore(alias)
        self.assertEqual(other.read_text(), "do not overwrite")
        first, fresh = self.store.reserve_card(self.RUN_ID, self.item, "-1001", "42", "9")
        second, repeated = self.store.reserve_card(self.RUN_ID, self.item, "-1001", "42", "9")
        self.assertEqual(first, second)
        self.assertTrue(fresh)
        self.assertFalse(repeated)

    def test_rating_rejects_invalid_unknown_and_finalized_cards(self):
        card_id, _ = self.store.reserve_card(self.RUN_ID, self.item, "-1001", "42", "9")
        self.store.mark_sent(card_id, 100)
        with self.assertRaisesRegex(ValueError, "invalid rating"):
            self.store.rate(card_id, "-1001", 100, "9", 4)
        with self.assertRaisesRegex(ValueError, "unknown digest card"):
            self.store.rate("missing", "-1001", 100, "9", 3)
        with self.store._connect() as db:
            db.execute("INSERT INTO completions (run_id,chat_id,thread_id,owner_id,path) "
                       "VALUES (?,?,?,?,?)", (self.RUN_ID, "-1001", "42", "9", "report.md"))
        with self.assertRaisesRegex(ValueError, "already finalized"):
            self.store.rate(card_id, "-1001", 100, "9", 3)

    def test_delivery_history_and_stats_only_include_owner_sent_cards(self):
        card_id, _ = self.store.reserve_card(self.RUN_ID, self.item, "-1001", "42", "9")
        self.assertEqual(self.store.recently_sent_items("9", "2000-01-01"), [])
        self.store.mark_sent(card_id, 100)
        self.assertEqual(self.store.card(card_id)["title"], self.item["title"])
        self.assertIsNone(self.store.card("missing"))
        self.assertEqual(self.store.recently_sent_items("9", "2000-01-01")[0]["url"],
                         self.item["url"])
        self.assertEqual(self.store.recently_sent_items("10", "2000-01-01"), [])
        self.assertEqual(self.store.stats("9")["delivered_cards"], 1)
        self.assertEqual(self.store.stats("10")["delivered_cards"], 0)

    def test_similar_good_news_ranks_above_bad_news(self):
        positive = dict(self.item)
        negative = {"title": "Stock options lawsuit", "url": "https://example.org/stocks",
                    "source_ids": ["hacker-news"], "category": "business",
                    "subcategory": "Equity", "evidence": "Old options dispute"}
        for item, score, message_id in ((positive, 3, 100), (negative, 1, 101)):
            card_id, _ = self.store.reserve_card(self.RUN_ID, item, "-1001", "42", "9")
            self.store.mark_sent(card_id, message_id)
            self.store.rate(card_id, "-1001", message_id, "9", score)
        history = self.store.history("9")
        next_tool = dict(positive, url="https://example.org/tool-2", title="Another agent toolkit")
        next_stocks = dict(negative, url="https://example.org/stocks-2", title="More stock options")
        self.assertGreater(preference_bonus(next_tool, history), 0)
        self.assertLess(preference_bonus(next_stocks, history), 0)
        self.assertGreater(preference_bonus(next_tool, history), preference_bonus(next_stocks, history))

    def test_delivery_sends_each_item_once_and_records_message(self):
        raw = self.state / f"raw-{self.RUN_ID}.json"
        raw.write_text(json.dumps({"run_id": self.RUN_ID, "items": [self.item]}))
        (self.state / f"staged-{self.RUN_ID}.md").write_text("staged")
        send = Mock(return_value=100)
        target = {"platform": "telegram", "chat_id": "-1001", "thread_id": "42"}
        deliver_cards(raw, self.state, target, "9", send)
        deliver_cards(raw, self.state, target, "9", send)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0:2], ("-1001", "42"))
        buttons = send.call_args.args[3]["inline_keyboard"][0]
        self.assertEqual([button["text"] for button in buttons], ["1", "2", "3"])
        self.assertTrue(all(button["callback_data"].startswith("nd:") for button in buttons))
        self.assertEqual(self.store.card_for_message("-1001", 100)["title"], self.item["title"])

    def test_delivery_stages_valid_draft_when_agent_skipped_finalizer(self):
        raw = self.state / f"raw-{self.RUN_ID}.json"
        item = dict(self.item, urls=[self.item["url"]])
        raw.write_text(json.dumps({"run_id": self.RUN_ID, "items": [item],
                                   "source_issues": []}))
        draft = self.state / f"draft-{self.RUN_ID}.md"
        draft.write_text("""# AI/IT News Digest

## 1. New agent toolkit
Sources: https://example.org/tool
### Junior
Это новый инструмент. Источник: https://example.org/tool
### Senior
Можно оценить применимость. Источник: https://example.org/tool
### Manager
Можно запланировать проверку. Источник: https://example.org/tool
""")
        target = {"platform": "telegram", "chat_id": "-1001", "thread_id": "42"}
        with patch("feedback.telegram_send", return_value={"message_id": 100}) as send:
            self.assertEqual(deliver_cards_to_telegram(raw, self.state, target,
                                                        "9", "test-token"), 1)
            self.assertEqual(deliver_cards_to_telegram(raw, self.state, target,
                                                        "9", "test-token"), 0)
        self.assertTrue((self.state / f"staged-{self.RUN_ID}.md").is_file())
        send.assert_called_once()

    def test_delivery_marks_missing_draft_without_inventing_analysis(self):
        raw = self.state / f"raw-{self.RUN_ID}.json"
        raw.write_text(json.dumps({"run_id": self.RUN_ID, "items": [self.item]}))
        target = {"platform": "telegram", "chat_id": "-1001", "thread_id": "42"}
        with patch("feedback.telegram_send", return_value={"message_id": 100}) as send:
            self.assertEqual(deliver_cards_to_telegram(raw, self.state, target,
                                                        "9", "test-token"), 1)
        self.assertIn("Анализ недоступен", send.call_args.args[2]["text"])
        self.assertIn("A coding agent toolkit", send.call_args.args[2]["text"])

    def test_card_uses_source_abstract_when_analysis_is_missing(self):
        from feedback import _card_text

        item = dict(self.item, evidence="The paper introduces a split mixture of experts for video diffusion.",
                    evidence_kind="abstract")
        card = _card_text(item, 1, 1, "Анализ недоступен. Откройте источник по ссылке в карточке.")
        self.assertIn("split mixture of experts", card)
        self.assertIn("аннотации", card)

    def test_delivery_rejects_empty_source_description_before_sending_any_card(self):
        raw = self.state / f"raw-{self.RUN_ID}.json"
        first = dict(self.item, evidence="A coding agent toolkit")
        second = dict(self.item, title="Unknown item", url="https://example.org/unknown",
                      evidence="")
        raw.write_text(json.dumps({"run_id": self.RUN_ID, "items": [first, second]}))
        send = Mock(return_value=100)
        target = {"platform": "telegram", "chat_id": "-1001", "thread_id": "42"}
        with self.assertRaisesRegex(ValueError, "source description unavailable for item 2"):
            deliver_cards(raw, self.state, target, "9", send)
        send.assert_not_called()

    def test_delivery_marks_invalid_draft_without_inventing_analysis(self):
        raw = self.state / f"raw-{self.RUN_ID}.json"
        raw.write_text(json.dumps({"run_id": self.RUN_ID, "items": [self.item]}))
        (self.state / f"draft-{self.RUN_ID}.md").write_text("Incomplete analysis")
        target = {"platform": "telegram", "chat_id": "-1001", "thread_id": "42"}
        with patch("feedback.telegram_send", return_value={"message_id": 100}) as send:
            self.assertEqual(deliver_cards_to_telegram(raw, self.state, target,
                                                        "9", "test-token"), 1)
        self.assertIn("Анализ недоступен", send.call_args.args[2]["text"])

    def test_partial_draft_sends_all_cards_with_explicit_missing_analysis(self):
        raw = self.state / f"raw-{self.RUN_ID}.json"
        first = dict(self.item, urls=[self.item["url"]])
        second = dict(self.item, title="Other release", url="https://example.org/other",
                      urls=["https://example.org/other"])
        raw.write_text(json.dumps({"run_id": self.RUN_ID, "items": [first, second],
                                   "source_issues": []}))
        (self.state / f"draft-{self.RUN_ID}.md").write_text("""# AI/IT News Digest

## 1. New agent toolkit
Sources: https://example.org/tool
### Junior
Проверенный анализ первой новости: https://example.org/tool
### Senior
Можно изучить инструмент: https://example.org/tool
### Manager
Можно оценить внедрение: https://example.org/tool
""")
        target = {"platform": "telegram", "chat_id": "-1001", "thread_id": "42"}
        with patch("feedback.telegram_send", side_effect=[{"message_id": 100},
                                                         {"message_id": 101}]) as send, \
                patch("feedback.time.sleep"):
            self.assertEqual(deliver_cards_to_telegram(raw, self.state, target,
                                                        "9", "test-token"), 2)
        self.assertIn("Проверенный анализ первой новости", send.call_args_list[0].args[2]["text"])
        self.assertIn("Анализ недоступен", send.call_args_list[1].args[2]["text"])
        staged = (self.state / f"staged-{self.RUN_ID}.md").read_text()
        self.assertIn("Проверенный анализ первой новости", staged)
        self.assertIn("## 2. Other release", staged)
        self.assertIn("Анализ недоступен", staged)
        first_card = self.store.card_for_message("-1001", 100)
        second_card = self.store.card_for_message("-1001", 101)
        self.store.rate(first_card["card_id"], "-1001", 100, "9", 3)
        self.store.rate(second_card["card_id"], "-1001", 101, "9", 3)
        report = complete_if_ready(self.store, second_card["card_id"],
                                   self.state, self.state / "output")
        self.assertIn("Анализ недоступен", report.read_text())

    def test_marker_is_only_accepted_for_digest_job_and_state_file(self):
        raw = self.state / f"raw-{self.RUN_ID}.json"
        raw.write_text("{}")
        content = f"Summary\nNEWS_CARDS:{raw}\nMEDIA:/tmp/report.md"
        clean, path = extract_cards_marker(content, {"skill": "ai_digest"}, self.state)
        self.assertEqual(path, raw.resolve())
        self.assertNotIn("NEWS_CARDS:", clean)
        clean, path = extract_cards_marker(content, {"skill": "other"}, self.state)
        self.assertIsNone(path)
        self.assertNotIn("NEWS_CARDS:", clean)

    def test_marker_uses_raw_item_count_instead_of_agent_estimate(self):
        raw = self.state / f"raw-{self.RUN_ID}.json"
        raw.write_text(json.dumps({"run_id": self.RUN_ID, "items": [self.item] * 5}))
        content = f"Проанализировано 3 предметов. Оцените карточки.\nNEWS_CARDS:{raw}"
        clean, path = extract_cards_marker(content, {"skill": "ai_digest"}, self.state)
        self.assertEqual(path, raw.resolve())
        self.assertIn("Собрано 5 новостей", clean)
        self.assertNotIn("Проанализировано 3", clean)

    def test_final_report_waits_for_each_card_vote_and_keeps_only_threes(self):
        from finalize_digest import stage

        first = dict(self.item, urls=[self.item["url"]])
        second = dict(self.item, title="Other item", url="https://example.org/other",
                      urls=["https://example.org/other"])
        raw = {"run_id": self.RUN_ID, "items": [first, second], "source_issues": []}
        (self.state / f"raw-{self.RUN_ID}.json").write_text(json.dumps(raw))
        draft = """# AI/IT News Digest

## 1. New agent toolkit
Sources: https://example.org/tool
### Junior
Useful to try.
### Senior
Useful to evaluate.
### Manager
Useful to plan.

## 2. Other item
Sources: https://example.org/other
### Junior
Other details.
### Senior
Other evaluation.
### Manager
Other planning.
"""
        stage(raw, draft, self.state)
        first_id, _ = self.store.reserve_card(self.RUN_ID, self.item, "-1001", "42", "9", 0)
        second_id, _ = self.store.reserve_card(self.RUN_ID, second, "-1001", "42", "9", 1)
        self.store.mark_sent(first_id, 100)
        self.store.mark_sent(second_id, 101)
        output = self.state / "output"
        self.store.rate(first_id, "-1001", 100, "9", 3)
        self.assertIsNone(complete_if_ready(self.store, first_id, self.state, output))
        self.store.rate(second_id, "-1001", 101, "9", 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: complete_if_ready(self.store, second_id, self.state, output),
                range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        report = next(result for result in results if result is not None)
        self.assertIsNotNone(report)
        self.assertIn("New agent toolkit", report.read_text())
        self.assertNotIn("Other item", report.read_text())
        self.assertIsNone(complete_if_ready(self.store, second_id, self.state, output))
        with self.assertRaisesRegex(ValueError, "finalized"):
            self.store.rate(second_id, "-1001", 101, "9", 3)

    def test_telegram_error_does_not_expose_bot_token(self):
        token = "123456:private"
        error = HTTPError(f"https://api.telegram.org/bot{token}/sendMessage", 403,
                          "Forbidden", {}, None)
        with patch("feedback.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                telegram_send(token, "sendMessage", {"chat_id": 1, "text": "hello"})
        self.assertNotIn(token, str(caught.exception))

    def test_marker_rejects_multiple_and_external_paths_but_keeps_private_delivery(self):
        raw = self.state / f"raw-{self.RUN_ID}.json"
        raw.write_text(json.dumps({"run_id": self.RUN_ID, "items": [self.item]}))
        marker = f"NEWS_CARDS:{raw}"
        clean, path = extract_cards_marker(f"Summary\n{marker}", {"skill": "ai_digest"}, self.state)
        self.assertEqual(path, raw.resolve())
        self.assertIn("Собрано 1 новость", clean)
        self.assertNotIn(str(raw), clean)
        for content in (f"{marker}\n{marker}", "NEWS_CARDS:/tmp/raw-invalid.json"):
            clean, path = extract_cards_marker(content, {"skill": "ai_digest"}, self.state)
            self.assertIsNone(path)
            self.assertNotIn("NEWS_CARDS:", clean)
        outside = self.state.parent / f"raw-{self.RUN_ID}.json"
        clean, path = extract_cards_marker(f"NEWS_CARDS:{outside}",
                                           {"skill": "ai_digest"}, self.state)
        self.assertIsNone(path)
        self.assertEqual(clean, "")
        raw.write_text("invalid json")
        clean, path = extract_cards_marker(marker, {"skill": "ai_digest"}, self.state)
        self.assertEqual(path, raw.resolve())

    def test_telegram_retries_429_once_and_sanitizes_transport_errors(self):
        rate_limited = HTTPError("https://api.telegram.org/botsecret/sendMessage", 429,
                                 "rate limit", {"Retry-After": "2"}, None)
        with patch("feedback.urlopen", side_effect=[rate_limited,
                                                    io.BytesIO(b'{"ok":true,"result":{"message_id":7}}')]) as request, \
                patch("feedback.time.sleep") as sleep:
            self.assertEqual(telegram_send("secret", "sendMessage", {"chat_id": 1})["message_id"], 7)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(2)
        with patch("feedback.urlopen", side_effect=URLError("secret")):
            with self.assertRaisesRegex(RuntimeError, "URLError") as caught:
                telegram_send("secret", "sendMessage", {"chat_id": 1})
        self.assertNotIn("secret", str(caught.exception))
        with patch("feedback.urlopen", return_value=io.BytesIO(b'{"ok":false}')):
            with self.assertRaisesRegex(RuntimeError, "returned an error"):
                telegram_send("secret", "sendMessage", {"chat_id": 1})

    def test_delivery_rejects_invalid_run(self):
        raw = self.state / f"raw-{self.RUN_ID}.json"
        raw.write_text(json.dumps({"run_id": "bad", "items": [self.item]}))
        mock_telegram = Mock()
        with self.assertRaisesRegex(ValueError, "invalid digest run"):
            deliver_cards(raw, self.state, {"chat_id": "1"}, "9", mock_telegram)
        mock_telegram.assert_not_called()

    def test_delivery_rejects_invalid_run_and_missing_token_before_send(self):
        """Combined test for invalid run and missing token scenarios."""
        # Test invalid run
        raw1 = self.state / f"raw-{self.RUN_ID}-1.json"
        raw1.write_text(json.dumps({"run_id": "bad", "items": [self.item]}))
        mock_telegram1 = Mock()
        with self.assertRaisesRegex(ValueError, "invalid digest run"):
            deliver_cards(raw1, self.state, {"chat_id": "1"}, "9", mock_telegram1)
        mock_telegram1.assert_not_called()

        # Test missing token
        raw2 = self.state / f"raw-{self.RUN_ID}-2.json"
        raw2.write_text(json.dumps({"run_id": self.RUN_ID, "items": [self.item]}))
        mock_telegram2 = Mock()
        with self.assertRaisesRegex(RuntimeError, "token unavailable"):
            deliver_cards_to_telegram(raw2, self.state, {"chat_id": "1"}, "9", "")
        mock_telegram2.assert_not_called()

    def test_delivery_rejects_empty_card_count(self):
        raw = self.state / f"raw-{self.RUN_ID}.json"
        raw.write_text(json.dumps({"run_id": self.RUN_ID, "items": []}))
        mock_telegram = Mock()
        with self.assertRaisesRegex(ValueError, "invalid digest card count"):
            deliver_cards(raw, self.state, {"chat_id": "1"}, "9", mock_telegram)
        mock_telegram.assert_not_called()

    def test_delivery_rejects_missing_token(self):
        raw = self.state / f"raw-{self.RUN_ID}.json"
        raw.write_text(json.dumps({"run_id": self.RUN_ID, "items": [self.item]}))
        mock_telegram = Mock()
        with self.assertRaisesRegex(RuntimeError, "token unavailable"):
            deliver_cards_to_telegram(raw, self.state, {"chat_id": "1"}, "9", "")
        mock_telegram.assert_not_called()
        from feedback import main

        jobs_path = self.state / "jobs.json"
        self.assertIsNone(owner_for_digest_jobs(jobs_path))
        jobs_path.write_text(json.dumps({"jobs": [
            {"skill": "ai_digest", "enabled": True, "origin": {"user_id": "9"}},
            {"skill": "ai_digest", "enabled": False, "origin": {"user_id": "10"}},
        ]}))
        self.assertEqual(owner_for_digest_jobs(jobs_path), "9")
        jobs_path.write_text(json.dumps({"jobs": [
            {"skill": "ai_digest", "origin": {"user_id": "9"}},
            {"skill": "ai_digest", "origin": {"user_id": "10"}},
        ]}))
        self.assertIsNone(owner_for_digest_jobs(jobs_path))
        output = io.StringIO()
        with patch.dict("os.environ", {"AI_DIGEST_STATE_DIR": str(self.state),
                                    "HERMES_HOME": str(self.state)}), redirect_stdout(output):
            self.assertEqual(main(["--stats"]), 2)
        self.assertEqual(json.loads(output.getvalue())["error"], "digest owner is ambiguous")
        output = io.StringIO()
        with patch.dict("os.environ", {"AI_DIGEST_STATE_DIR": str(self.state),
                                    "HERMES_HOME": str(self.state)}), redirect_stdout(output):
            self.assertEqual(main(["--stats", "--user-id", "9"]), 0)
        self.assertEqual(json.loads(output.getvalue())["rated_items"], 0)

    def test_missing_finalizer_and_unknown_completion_are_explicit_errors(self):
        import feedback

        with patch("feedback.importlib.util.spec_from_file_location", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "finalizer unavailable"):
                feedback._finalizer()
        with self.assertRaisesRegex(ValueError, "unknown digest card"):
            complete_if_ready(self.store, "missing", self.state, self.state / "output")

    def test_completion_message_is_recorded_and_unrelated_ratings_do_not_bias(self):
        with self.store._connect() as db:
            db.execute("INSERT INTO completions (run_id,chat_id,thread_id,owner_id,path) "
                       "VALUES (?,?,?,?,?)", (self.RUN_ID, "-1001", "42", "9", "report.md"))
        self.store.mark_completion_sent(self.RUN_ID, "-1001", "42", "9", 777)
        with self.store._connect() as db:
            message = db.execute("SELECT message_id FROM completions").fetchone()[0]
        self.assertEqual(message, 777)
        self.assertEqual(preference_bonus(self.item, []), 0)
        same = dict(self.item, score=3, updated_at="bad date")
        self.assertEqual(preference_bonus(self.item, [same]), 0)
        similar = dict(self.item, url="https://example.org/old", score=3,
                       updated_at="bad date")
        self.assertGreater(preference_bonus(self.item, [similar]), 0)

    def test_two_item_marker_uses_correct_noun(self):
        raw = self.state / f"raw-{self.RUN_ID}.json"
        raw.write_text(json.dumps({"run_id": self.RUN_ID, "items": [self.item, self.item]}))
        clean, path = extract_cards_marker(f"NEWS_CARDS:{raw}",
                                           {"skill": "ai_digest"}, self.state)
        self.assertEqual(path, raw.resolve())
        self.assertIn("Собрано 2 новости", clean)

    def test_telegram_caps_malformed_retry_after(self):
        rate_limited = HTTPError("https://api.telegram.org/botsecret/sendMessage", 429,
                                 "rate limit", {"Retry-After": "invalid"}, None)
        with patch("feedback.urlopen", side_effect=[rate_limited,
                                                    io.BytesIO(b'{"ok":true,"result":{}}')]), \
                patch("feedback.time.sleep") as sleep:
            self.assertEqual(telegram_send("secret", "sendMessage", {"chat_id": 1}), {})
        sleep.assert_called_once_with(1)

    def test_concurrent_staging_accepts_winner_only_when_file_exists(self):
        import feedback

        finalizer = Mock()
        finalizer.stage.side_effect = FileExistsError("concurrent stage")
        with self.assertRaises(FileExistsError):
            feedback._stage_cards(finalizer, {}, [self.item], self.state, self.RUN_ID)
        staged = self.state / f"staged-{self.RUN_ID}.md"

        def winner(*_args):
            staged.write_text("completed")
            raise FileExistsError("concurrent stage")

        finalizer.stage.side_effect = winner
        self.assertEqual(feedback._stage_cards(finalizer, {}, [self.item],
                                               self.state, self.RUN_ID), staged)


if __name__ == "__main__":
    unittest.main()

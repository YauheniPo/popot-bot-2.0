"""A cron success requires artifacts from this execution, not a model's promise."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "ai_digest_run", Path(__file__).with_name("ai_digest_run.py"))
digest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(digest)
SKILL_DIR = Path(__file__).resolve().parents[1] / "skills/ai_digest"


class DigestRunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.state = self.home / "ops/news"
        self.env = mock.patch.dict(os.environ, {
            "AI_DIGEST_SKILL_DIR": str(SKILL_DIR),
            "AI_DIGEST_STATE_DIR": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.guard = digest.begin_run({"skills": ["ai_digest"]}, self.home)
        self.raw = {"run_id": self.guard.run_id, "items": [{
            "title": "New toolkit", "url": "https://example.org/tool",
            "urls": ["https://example.org/tool"], "evidence": "Source description",
        }], "source_issues": []}
        self.draft = ("# Digest\n\n## 1. New toolkit\n"
                      "### Junior\nВыпущен набор инструментов.\n"
                      "### Senior\nИнтерпретация агента: пригодится для прототипа.\n"
                      "### Manager\nИнтерпретация агента: оцените затраты внедрения.\n"
                      "Sources: https://example.org/tool\n")

    def write_artifacts(self, draft=True):
        self.state.mkdir(parents=True, exist_ok=True)
        self.guard.raw_path.write_text(json.dumps(self.raw))
        if draft:
            self.guard.draft_path.write_text(self.draft)

    def test_valid_artifacts_produce_marker_without_model_final_prose(self):
        self.write_artifacts()
        repair = mock.Mock()
        result = self.guard.finalize_with_repair(repair)
        self.assertIn(f"NEWS_CARDS:{self.guard.raw_path}", result)
        self.assertTrue(self.guard.staged_path.is_file())
        self.assertEqual(self.guard.finalize(), result)
        self.assertNotIn("доставлен", result)
        repair.assert_not_called()

    def test_missing_collection_never_reuses_an_old_run(self):
        self.state.mkdir(parents=True)
        (self.state / "raw-20260101-000000-abcdef12.json").write_text(json.dumps(self.raw))
        repair = mock.Mock()
        with self.assertRaisesRegex(ValueError, "collection missing"):
            self.guard.finalize_with_repair(repair)
        repair.assert_not_called()

    def test_missing_analysis_gets_one_bounded_correction(self):
        self.write_artifacts(draft=False)
        repair = mock.Mock(side_effect=lambda prompt: self.guard.draft_path.write_text(self.draft))
        self.assertIn("NEWS_CARDS:", self.guard.finalize_with_repair(repair))
        repair.assert_called_once()
        self.assertIn("Do not repeat collection", repair.call_args.args[0])

    def test_failed_correction_is_not_success(self):
        self.write_artifacts(draft=False)
        repair = mock.Mock()
        with self.assertRaisesRegex(ValueError, "analysis missing"):
            self.guard.finalize_with_repair(repair)
        repair.assert_called_once()
        self.assertFalse(self.guard.staged_path.exists())

    def test_identity_and_empty_collection_do_not_trigger_research_retry(self):
        for change in ({"run_id": "20260101-000000-abcdef12"}, {"items": []},
                       {"items": ["invalid"]}):
            with self.subTest(change=change):
                self.write_artifacts()
                self.guard.raw_path.write_text(json.dumps({**self.raw, **change}))
                repair = mock.Mock()
                with self.assertRaises(ValueError):
                    self.guard.finalize_with_repair(repair)
                repair.assert_not_called()

    def test_symlink_artifact_cannot_be_repaired_or_published(self):
        self.write_artifacts()
        outside = self.home / "private.md"
        outside.write_text(self.draft)
        self.guard.draft_path.unlink()
        self.guard.draft_path.symlink_to(outside)
        repair = mock.Mock()
        with self.assertRaisesRegex(ValueError, "unsafe artifact"):
            self.guard.finalize_with_repair(repair)
        repair.assert_not_called()

    def test_staged_analysis_is_revalidated_before_delivery(self):
        self.write_artifacts()
        self.guard.finalize()
        self.guard.staged_path.write_text("All cards ready")
        with self.assertRaises(ValueError):
            self.guard.finalize()

    def test_each_execution_has_a_unique_contract_and_leaves_job_unchanged(self):
        job = {"skill": "ai_digest", "deliver": "origin", "prompt": "daily"}
        guard = digest.begin_run(job, self.home)
        self.assertNotEqual(self.guard.run_id, guard.run_id)
        self.assertIn(f"--run-id {guard.run_id}", guard.instructions("keep this request"))
        self.assertIn("keep this request", guard.instructions("keep this request"))
        self.assertEqual(job, {"skill": "ai_digest", "deliver": "origin", "prompt": "daily"})

    def test_other_jobs_and_no_agent_are_unchanged(self):
        for job in ({}, {"skills": ["other"]}, {"no_agent": True, "skill": "ai_digest"}):
            self.assertIsNone(digest.begin_run(job, self.home))

    def test_missing_skill_configuration_never_imports_from_working_directory(self):
        for skill_dir in ("", "relative/ai_digest", str(self.home / "missing")):
            with self.subTest(skill_dir=skill_dir), mock.patch.dict(
                    os.environ, {"AI_DIGEST_SKILL_DIR": skill_dir}):
                with self.assertRaisesRegex(ValueError, "managed skill unavailable"):
                    digest.begin_run({"skill": "ai_digest"}, self.home)

    def test_analysis_encoding_and_size_errors_are_bounded(self):
        self.write_artifacts()
        for content in (b"\xff", b"x" * 129):
            with self.subTest(content_length=len(content)):
                self.guard.draft_path.write_bytes(content)
                # Only lower the analysis read cap; raw input still uses its real cap.
                with mock.patch.object(self.guard.finalizer, "MAX_RAW_BYTES", 128):
                    with self.assertRaises(digest.AnalysisError):
                        self.guard._analysis(self.guard.draft_path)

    def test_incompatible_report_skills_are_rejected(self):
        job = {"skills": ["ai_digest", "competitor-news-monitor"]}
        with self.assertRaisesRegex(ValueError, "separate cron jobs"):
            digest.begin_run(job, self.home)


if __name__ == "__main__":
    unittest.main()

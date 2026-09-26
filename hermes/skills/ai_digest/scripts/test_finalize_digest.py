"""The report gate must fail closed before a cron delivery is attempted."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from finalize_digest import finalize, _validate_output_dir, _validate_report, _validate_source_availability  # noqa: E402


RAW = {"run_id": "20260925-090000-abcdef12", "items": [
    {"title": "AI release", "urls": ["https://vendor.test/release"]}]}
VALID = """# AI/IT News Digest

## 1. AI release

Sources: https://vendor.test/release

### Junior
What happened. Use: read the release.

### Senior
What changed. Use: evaluate it.

### Manager
Impact. Use: estimate cost.
"""


class FinalizeDigestTests(unittest.TestCase):
    def test_writes_exclusive_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = finalize(RAW, VALID, Path(directory))
            self.assertEqual(path.read_text(), VALID)
            with self.assertRaises(FileExistsError):
                finalize(RAW, VALID, Path(directory))

    def test_creates_missing_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "new" / "digests"
            path = finalize(RAW, VALID, output_dir)
            self.assertTrue(output_dir.is_dir())
            self.assertEqual(path.read_text(), VALID)

    def test_rejects_missing_role(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid_draft = VALID.replace("### Senior", "### Expert")
            output_dir = Path(directory)
            with self.assertRaises(ValueError):
                finalize(RAW, invalid_draft, output_dir)

    def test_rejects_fabricated_link(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                finalize(RAW, VALID + "\nhttps://unseen.test/claim\n", Path(directory))

    def test_requires_every_link_for_a_merged_story(self):
        raw = {"run_id": RAW["run_id"], "items": [
            {"title": "AI release", "urls": ["https://vendor.test/release",
                                              "https://news.test/discussion"]}]}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                finalize(raw, VALID, Path(directory))

    def test_requires_failed_source_in_availability_block(self):
        raw = {**RAW, "source_issues": [
            {"id": "reddit", "kind": "failed", "reason": "HTTP 403"}]}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                finalize(raw, VALID, Path(directory))

    def test_validate_output_dir_rejects_traversal(self):
        with self.assertRaises(ValueError):
            _validate_output_dir(Path("/tmp/../evil"))
        with self.assertRaises(ValueError):
            _validate_output_dir(Path(".."))
        with tempfile.TemporaryDirectory() as directory:
            normalized_path = Path(directory) / ".." / Path(directory).name
            self.assertEqual(normalized_path.resolve(), Path(directory).resolve())
            with self.assertRaises(ValueError):
                _validate_output_dir(normalized_path)

    def test_validate_output_dir_accepts_absolute(self):
        with tempfile.TemporaryDirectory() as directory:
            result = _validate_output_dir(Path(directory))
            self.assertEqual(result, Path(directory).resolve())

    def test_validate_report_rejects_invalid_run_id(self):
        with self.assertRaises(ValueError):
            _validate_report({"run_id": "bad-format"}, VALID, RAW["items"])

    def test_validate_report_rejects_empty_items(self):
        with self.assertRaises(ValueError):
            _validate_report({**RAW, "run_id": "20260925-090000-abcdef12"}, VALID, [])

    def test_validate_report_rejects_mismatched_headings(self):
        draft = "## 1. Wrong Title\n\n### Junior\nA\n### Senior\nB\n### Manager\nC"
        with self.assertRaises(ValueError):
            _validate_report(RAW, draft, RAW["items"])

    def test_validate_report_rejects_count_mismatch(self):
        draft = "## 1. Item 1\n\n### Junior\nA\n### Senior\nB\n### Manager\nC"
        with self.assertRaises(ValueError):
            _validate_report(RAW, draft, [{"title": "Item 1"}, {"title": "Item 2"}])

    def test_validate_report_rejects_wrong_title(self):
        draft = ("# Digest\n\n## 1. Wrong\n\n### Junior\nA\n### Senior\nB\n### Manager\nC\n\n"
                 "## Source availability\n\n### Sources\nreddit unavailable")
        with self.assertRaises(ValueError):
            _validate_report(RAW, draft, RAW["items"])

    def test_validate_source_availability_ok_with_empty_issues(self):
        _validate_source_availability(VALID, [])

    def test_validate_source_availability_rejects_missing(self):
        raw = {**RAW, "source_issues": [{"id": "reddit", "kind": "failed", "reason": "error"}]}
        draft = VALID + "\n## Source availability\n\n### Sources\n"
        with self.assertRaises(ValueError):
            _validate_source_availability(draft, raw["source_issues"])

    def test_validate_source_availability_rejects_malformed_issue(self):
        draft = VALID + "\n## Source availability\n\n### Sources\n"
        with self.assertRaises(ValueError):
            _validate_source_availability(draft, [{"kind": "failed", "reason": "error"}])

    def test_validate_source_availability_ok_with_documented_issue(self):
        draft = VALID + "\n## Source availability\n\n### Sources\nreddit unavailable\n"
        _validate_source_availability(draft, [{"id": "reddit", "kind": "failed", "reason": "error"}])

    def test_main_writes_digest_and_returns_zero(self):
        import json as _json
        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "raw.json"
            raw_path.write_text(_json.dumps({**RAW, "source_issues": []}))
            draft_path = Path(directory) / "draft.md"
            draft_path.write_text(VALID)
            output = Path(directory) / "out"
            with patch.dict("os.environ", {"AI_DIGEST_OUTPUT_DIR": str(output),
                                             "AI_DIGEST_STATE_DIR": directory}):
                from finalize_digest import main
                result = main(["--raw", str(raw_path), "--draft", str(draft_path)])
            self.assertEqual(result, 0)
            self.assertTrue((output / "digest-20260925-090000-abcdef12.md").exists())

    def test_main_rejects_output_directory_override(self):
        import json as _json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_path = root / "raw.json"
            raw_path.write_text(_json.dumps({**RAW, "source_issues": []}))
            draft_path = root / "draft.md"
            draft_path.write_text(VALID)
            configured_output = root / "configured"
            override_output = root / "override"
            with patch.dict("os.environ", {"AI_DIGEST_OUTPUT_DIR": str(configured_output),
                                             "AI_DIGEST_STATE_DIR": str(root)}):
                with self.assertRaises(SystemExit):
                    from finalize_digest import main
                    main(["--raw", str(raw_path), "--draft", str(draft_path),
                          "--output-dir", str(override_output)])
            self.assertFalse(configured_output.exists())
            self.assertFalse(override_output.exists())

    def test_finalize_cleans_temp_after_link_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("os.link", side_effect=OSError("cross-device link")):
                with self.assertRaises(OSError):
                    finalize(RAW, VALID, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_finalize_stages_in_private_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            link = os.link

            def verify_private_staging(source, target):
                staging = Path(source).parent
                self.assertEqual(staging.parent, Path(directory).resolve())
                self.assertEqual(staging.stat().st_mode & 0o777, 0o700)
                self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
                link(source, target)

            with patch("os.link", side_effect=verify_private_staging):
                finalize(RAW, VALID, Path(directory))
            self.assertEqual([path.name for path in Path(directory).iterdir()], [
                "digest-20260925-090000-abcdef12.md"])

    def test_main_rejects_oversized_raw_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_path = root / "raw.json"
            raw_path.write_text('"' + ("x" * 10_485_760) + '"', encoding="utf-8")
            draft_path = root / "draft.md"
            draft_path.write_text(VALID, encoding="utf-8")
            with patch.dict("os.environ", {"AI_DIGEST_STATE_DIR": str(root)}):
                with self.assertRaisesRegex(ValueError, "raw file too large"):
                    from finalize_digest import main
                    main(["--raw", str(raw_path), "--draft", str(draft_path)])

    def test_main_rejects_raw_file_outside_state_directory(self):
        import json as _json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            raw_path = root / "outside.json"
            raw_path.write_text(_json.dumps(RAW), encoding="utf-8")
            draft_path = state_dir / "draft.md"
            draft_path.write_text(VALID, encoding="utf-8")
            with patch.dict("os.environ", {"AI_DIGEST_STATE_DIR": str(state_dir)}):
                with self.assertRaisesRegex(ValueError, "outside state directory"):
                    from finalize_digest import main
                    main(["--raw", str(raw_path), "--draft", str(draft_path)])

    def test_main_rejects_raw_directory_inside_state_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            raw_path = state_dir / "raw-dir"
            raw_path.mkdir()
            draft_path = state_dir / "draft.md"
            draft_path.write_text(VALID, encoding="utf-8")
            with patch.dict("os.environ", {"AI_DIGEST_STATE_DIR": str(state_dir)}):
                with self.assertRaisesRegex(ValueError, "input path is not a file"):
                    from finalize_digest import main
                    main(["--raw", str(raw_path), "--draft", str(draft_path)])

    def test_main_rejects_non_object_raw_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_path = root / "raw.json"
            raw_path.write_text("[]", encoding="utf-8")
            draft_path = root / "draft.md"
            draft_path.write_text(VALID, encoding="utf-8")
            with patch.dict("os.environ", {"AI_DIGEST_STATE_DIR": str(root)}):
                with self.assertRaisesRegex(ValueError, "JSON object"):
                    from finalize_digest import main
                    main(["--raw", str(raw_path), "--draft", str(draft_path)])

    def test_finalize_cleans_up_temp_on_exception(self):
        """Test that finalize cleans up temp file on BaseException during write."""
        with tempfile.TemporaryDirectory() as directory:
            with patch("os.fsync", side_effect=RuntimeError("disk full")):
                with self.assertRaises(RuntimeError):
                    finalize(RAW, VALID, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()

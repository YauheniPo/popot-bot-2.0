"""The report gate must fail closed before a cron delivery is attempted."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))

from finalize_digest import finalize  # noqa: E402


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

    def test_rejects_missing_role(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                finalize(RAW, VALID.replace("### Senior", "### Expert"), Path(directory))

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


if __name__ == "__main__":
    unittest.main()

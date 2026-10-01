"""The browser installer keeps recoverable runtimes without retaining old downloads."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("prune-install-caches.py")


def load_pruner():
    spec = importlib.util.spec_from_file_location("prune_install_caches", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PruneInstallCachesTests(unittest.TestCase):
    def test_keeps_two_newest_chrome_builds_and_one_in_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            root = home / ".agent-browser" / "browsers"
            root.mkdir(parents=True)
            for version in ("152.0.1.1", "153.0.1.1", "154.0.1.1", "154.0.1.2"):
                (root / f"chrome-{version}").mkdir()
            (root / "custom-profile").mkdir()
            removed = load_pruner().prune_browser_builds(root, {"chrome-152.0.1.1"})
            self.assertEqual(removed, 1)
            self.assertEqual(sorted(path.name for path in root.iterdir()), [
                "chrome-152.0.1.1", "chrome-154.0.1.1", "chrome-154.0.1.2", "custom-profile",
            ])
            self.assertEqual(load_pruner().prune_browser_builds(root, set()), 1)
            self.assertEqual(load_pruner().prune_browser_builds(root, set()), 0)

    def test_cache_prune_is_bounded_and_preserves_unknown_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            known = home / ".npm" / "_cacache"
            known.mkdir(parents=True)
            (known / "data").write_bytes(b"x" * 20)
            unknown = home / ".npm" / "private-not-cache"
            unknown.mkdir()
            (unknown / "keep").write_text("saved")
            pruner = load_pruner()
            self.assertEqual(pruner.prune_large_caches(home, max_bytes=21, busy=False), 0)
            self.assertTrue(known.exists())
            self.assertEqual(pruner.prune_large_caches(home, max_bytes=20, busy=True), 0)
            self.assertEqual(pruner.prune_large_caches(home, max_bytes=20, busy=False), 1)
            self.assertFalse(known.exists())
            self.assertEqual((unknown / "keep").read_text(), "saved")
            self.assertEqual(pruner.prune_large_caches(home, max_bytes=20, busy=False), 0)

    def test_rejects_symlinked_cache_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            outside = home / "outside"
            outside.mkdir()
            (outside / "keep").write_text("saved")
            cache = home / ".cache"
            cache.mkdir()
            (cache / "uv").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                load_pruner().prune_large_caches(home, max_bytes=0, busy=False)
            self.assertEqual((outside / "keep").read_text(), "saved")

    def test_browser_root_symlink_cannot_delete_another_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            outside = home / "outside"
            (outside / "chrome-1.0.0.0").mkdir(parents=True)
            (home / "browsers").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                load_pruner().prune_browser_builds(home / "browsers", set())
            self.assertTrue((outside / "chrome-1.0.0.0").exists())

    def test_installer_prunes_only_after_live_browser_check(self) -> None:
        installer = MODULE_PATH.with_name("install-browser-automation.sh").read_text()
        self.assertLess(installer.index(' snapshot\n'), installer.index(' close\n'))
        self.assertLess(installer.index(' close\n'), installer.index('"${SCRIPT_DIR}/prune-install-caches.py"'))


if __name__ == "__main__":
    unittest.main()

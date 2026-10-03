"""The browser installer keeps recoverable runtimes without retaining old downloads."""

from __future__ import annotations

import importlib.util
import io
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("prune-install-caches.py")


def load_pruner():
    spec = importlib.util.spec_from_file_location("prune_install_caches", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
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
            pruner = load_pruner()
            with self.assertRaises(ValueError):
                pruner.prune_large_caches(home, max_bytes=0, busy=False)
            self.assertEqual((outside / "keep").read_text(), "saved")

    def test_browser_root_symlink_cannot_delete_another_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            outside = home / "outside"
            (outside / "chrome-1.0.0.0").mkdir(parents=True)
            (home / "browsers").symlink_to(outside, target_is_directory=True)
            pruner = load_pruner()
            with self.assertRaises(ValueError):
                pruner.prune_browser_builds(home / "browsers", set())
            self.assertTrue((outside / "chrome-1.0.0.0").exists())

    def test_absent_browser_root_is_safe_on_first_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(load_pruner().prune_browser_builds(Path(temporary) / "browsers", set()), 0)

    def test_nested_cache_symlink_is_skipped(self) -> None:
        """Symlink inside cache is skipped (not followed) during size calculation."""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            cache = home / ".cache" / "pip"
            cache.mkdir(parents=True)
            outside = home / "outside"
            outside.write_text("keep")
            (cache / "link").symlink_to(outside)
            # No exception expected; symlink is skipped, size of target not counted
            load_pruner().prune_large_caches(home, max_bytes=0, busy=False)
            # Symlink file should still exist
            self.assertTrue((cache / "link").exists())
            # Outside file should be untouched
            self.assertEqual(outside.read_text(), "keep")
    def test_installer_prunes_only_after_live_browser_check(self) -> None:
        installer = MODULE_PATH.with_name("install-browser-automation.sh").read_text()
        self.assertLess(installer.index(' snapshot\n'), installer.index(' close\n'))
        self.assertLess(installer.index(' close\n'), installer.index('"${SCRIPT_DIR}/prune-install-caches.py"'))

    def test_detects_active_chrome_and_package_installers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            proc = Path(temporary) / "proc"
            chrome = home / ".agent-browser/browsers/chrome-152.0.1.1/chrome"
            chrome.parent.mkdir(parents=True)
            chrome.touch()
            browser = proc / "123"
            browser.mkdir(parents=True)
            (browser / "exe").symlink_to(chrome)
            (browser / "comm").write_text("chrome\n")
            installer = proc / "124"
            installer.mkdir()
            (installer / "exe").symlink_to("/usr/bin/node")
            (installer / "comm").write_text("node\n")
            (installer / "cmdline").write_bytes(b"node\0/usr/lib/node_modules/npm/bin/npm-cli.js\0install\0")
            self.assertEqual(load_pruner().active_processes(home, proc),
                             ({"chrome-152.0.1.1"}, True))

    def test_unavailable_process_table_is_not_a_complete_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            pruner = load_pruner()
            missing_proc = home / "missing-proc"
            with self.assertRaises(OSError):
                pruner.active_processes(home, missing_proc)

    def test_incomplete_process_scan_preserves_browser_builds_and_caches(self) -> None:
        for error in (PermissionError('private detail'), UnicodeError('private detail'),
                      FileNotFoundError('private detail')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                browser = home / '.agent-browser' / 'browsers'
                for version in ('1.0.0.0', '2.0.0.0', '3.0.0.0'):
                    (browser / f'chrome-{version}').mkdir(parents=True)
                cache = home / '.cache' / 'pip'
                cache.mkdir(parents=True)
                (cache / 'data').write_bytes(b'keep')
                pruner = load_pruner()
                with patch('sys.argv', ['prune-install-caches.py', '--user-home', str(home)]), \
                        patch.object(pruner, 'active_processes', side_effect=error), \
                        patch.object(pruner, 'MAX_CACHE_BYTES', 1), \
                        patch('sys.stderr', new_callable=io.StringIO) as warnings, \
                        patch('sys.stdout', new_callable=io.StringIO) as output:
                    pruner.main()
                self.assertEqual(len(list(browser.iterdir())), 3)
                self.assertEqual((cache / 'data').read_bytes(), b'keep')
                self.assertIn('cleanup deferred', warnings.getvalue())
                self.assertIn(type(error).__name__, warnings.getvalue())
                self.assertNotIn('private detail', warnings.getvalue())
                self.assertEqual(output.getvalue().strip(), 'browser_removed=0 cache_entries_removed=0')

    def test_unreadable_process_fields_propagate_instead_of_hiding_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            proc = home / 'proc'
            process = proc / '123'
            process.mkdir(parents=True)
            (process / 'exe').symlink_to('/usr/bin/node')
            (process / 'comm').write_text('node')
            (process / 'cmdline').write_bytes(b'node\0npm-cli.js\0install\0')
            pruner = load_pruner()
            for target, error in (('os.readlink', PermissionError()),
                                  ('pathlib.Path.read_text', PermissionError()),
                                  ('pathlib.Path.read_text', UnicodeError()),
                                  ('pathlib.Path.read_bytes', PermissionError())):
                with self.subTest(target=target, error=type(error).__name__), \
                        patch(target, side_effect=error), self.assertRaises(type(error)):
                    pruner.active_processes(home, proc)

    def test_process_scan_handles_exited_processes_and_other_installers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            proc = Path(temporary) / "proc"
            proc.mkdir()
            (proc / "self").mkdir()
            exited = proc / "123"
            exited.mkdir()
            interrupted = proc / "127"
            interrupted.mkdir()
            (interrupted / "exe").symlink_to("/usr/bin/node")
            (interrupted / "comm").write_text("node")
            for pid, exe, command, argv in (
                ("124", home / ".cache/ms-playwright/chrome", "chrome", b"chrome\0"),
                ("125", Path("/usr/bin/pip3"), "pip3", b"pip3\0install\0"),
                ("126", Path("/usr/bin/python3"), "python3", b"python3\0-m\0pip\0install\0"),
            ):
                process = proc / pid
                process.mkdir()
                (process / "exe").symlink_to(exe)
                (process / "comm").write_text(command)
                (process / "cmdline").write_bytes(argv)
            self.assertEqual(load_pruner().active_processes(home, proc), (set(), True))

    def test_main_prunes_after_process_check_and_is_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            browser = home / ".agent-browser" / "browsers"
            browser.mkdir(parents=True)
            for version in ("1.0.0.0", "2.0.0.0", "3.0.0.0"):
                (browser / f"chrome-{version}").mkdir()
            pruner = load_pruner()
            with patch("sys.argv", ["prune-install-caches.py", "--user-home", str(home)]), \
                    patch.object(pruner, "active_processes", return_value=(set(), False)), \
                    patch.object(pruner, "MAX_CACHE_BYTES", 1), \
                    patch("builtins.print") as output:
                pruner.main()
                output.assert_called_with("browser_removed=1 cache_entries_removed=0")
                pruner.main()
                output.assert_called_with("browser_removed=0 cache_entries_removed=0")

    def test_main_rejects_relative_home(self) -> None:
        pruner = load_pruner()
        with patch("sys.argv", ["prune-install-caches.py", "--user-home", "relative"]), \
                self.assertRaises(SystemExit) as error:
            pruner.main()
        self.assertEqual(error.exception.code, 2)

    def test_cli_accepts_a_clean_first_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch("sys.argv", ["prune-install-caches.py", "--user-home", temporary]), \
                    patch("builtins.print") as output:
                runpy.run_path(str(MODULE_PATH), run_name="__main__")
            output.assert_called_with("browser_removed=0 cache_entries_removed=0")


if __name__ == "__main__":
    unittest.main()

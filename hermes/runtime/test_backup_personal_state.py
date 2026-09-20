"""Personal state must round-trip without broadening filesystem access."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import zipfile


class BackupPersonalStateTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "backup_personal_state", Path(__file__).with_name("backup-personal-state.py")
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.workspace = self.root / "workspace"
        self.home.mkdir()
        self.workspace.mkdir()

    def put(self, root, name, content="personal text"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_nested_instructions_round_trip_and_stale_entries_disappear(self):
        self.put(self.workspace, "AGENTS.md")
        extra = self.put(self.workspace, "project/AGENTS.extra.md", "project rules")
        self.put(self.workspace, "project/.git/AGENTS.md", "excluded")
        self.module.mirror_workspace(self.home, self.workspace)
        destination = self.root / "replacement"
        self.module.restore_workspace(self.home, destination)
        self.assertEqual((destination / "project/AGENTS.extra.md").read_text(), "project rules")
        self.assertFalse((destination / "project/.git").exists())
        self.assertEqual((destination / "AGENTS.md").stat().st_mode & 0o777, 0o600)
        extra.unlink()
        self.module.mirror_workspace(self.home, self.workspace)
        other = self.root / "other"
        self.module.restore_workspace(self.home, other)
        self.assertFalse((other / "project/AGENTS.extra.md").exists())

    def test_rejects_symlinked_instruction_and_restore_destination(self):
        outside = self.put(self.root, "outside/AGENTS.md", "untouched")
        (self.workspace / "AGENTS.md").symlink_to(outside)
        with self.assertRaises((OSError, RuntimeError)):
            self.module.mirror_workspace(self.home, self.workspace)
        (self.workspace / "AGENTS.md").unlink()
        self.put(self.workspace, "child/AGENTS.md")
        self.module.mirror_workspace(self.home, self.workspace)
        destination = self.root / "replacement"
        destination.mkdir()
        (destination / "child").symlink_to(outside.parent, target_is_directory=True)
        with self.assertRaises((OSError, RuntimeError)):
            self.module.restore_workspace(self.home, destination)
        self.assertEqual(outside.read_text(), "untouched")

    def test_restore_validates_entire_manifest_before_writing(self):
        manifest = {"version": 1, "files": {"AGENTS.md": "new", "../AGENTS.md": "bad"}}
        self.put(self.home, "operator-state/workspace-instructions.json", json.dumps(manifest))
        self.put(self.workspace, "AGENTS.md", "original")
        with self.assertRaises(ValueError):
            self.module.restore_workspace(self.home, self.workspace)
        self.assertEqual((self.workspace / "AGENTS.md").read_text(), "original")

    def test_legacy_archive_without_instruction_manifest_is_noop(self):
        self.assertIsNone(self.module.restore_workspace(self.home, self.workspace))

    def test_legacy_archive_does_not_restore_stale_local_manifest(self):
        self.put(self.workspace, "AGENTS.md", "old")
        self.module.mirror_workspace(self.home, self.workspace)
        self.put(self.workspace, "AGENTS.md", "current")
        archive = self.root / "legacy.zip"
        with zipfile.ZipFile(archive, "w") as backup:
            backup.writestr("config.yaml", "{}")
        self.assertIsNone(self.module.restore_workspace(self.home, self.workspace, archive))
        self.assertEqual((self.workspace / "AGENTS.md").read_text(), "current")

    def test_rejects_hardlinked_instructions(self):
        source = self.put(self.root, "outside.md")
        os.link(source, self.workspace / "AGENTS.md")
        with self.assertRaises(RuntimeError):
            self.module.mirror_workspace(self.home, self.workspace)

    def test_symlinked_memories_cannot_leak_external_files(self):
        other = self.put(self.root, "outside/USER.md")
        (self.home / "memories").symlink_to(other.parent, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.module.personal_files(self.home)

    def test_inventory_includes_personal_profiles_not_heavy_runtime(self):
        required = ["SOUL.md", "memories/USER.md", "memories/MEMORY.md",
                    "profiles/custom/config.yaml", "profiles/custom/.env",
                    "profiles/custom/.managed-swarm",
                    "profiles/custom/SOUL.md", "profiles/custom/memories/MEMORY.md",
                    "profiles/custom/AGENTS.extra.md", "swarm/swarm.yaml",
                    "external_memory_providers.json"]
        for name in required:
            self.put(self.home, name)
        self.put(self.home, "profiles/custom/sessions/large.json")
        self.put(self.home, "profiles/custom/hermes-agent/main.py")
        self.assertEqual(set(self.module.personal_files(self.home)), set(required))

    def test_managed_profile_shared_credentials_are_backed_up_once(self):
        for name in (".env", "auth.json"):
            source = self.put(self.home, name, "test-only-secret")
            profile = self.home / "profiles/custom"
            profile.mkdir(parents=True, exist_ok=True)
            (profile / name).symlink_to(source)
        self.assertEqual(set(self.module.personal_files(self.home)), {".env", "auth.json"})
        (profile / ".env").unlink()
        (profile / ".env").symlink_to(self.put(self.root, "outside.env"))
        with self.assertRaises(RuntimeError):
            self.module.personal_files(self.home)

    def native(self, omit=None):
        native = SimpleNamespace(_QUICK_STATE_FILES=("config.yaml",),
                                 _QUICK_SNAPSHOTS_DIR="state-snapshots")
        def create(**kwargs):
            self.assertGreater(kwargs["keep"], 10000)
            snap = self.home / native._QUICK_SNAPSHOTS_DIR / "test-scheduled"
            snap.mkdir(parents=True)
            files = {}
            for rel in native._QUICK_STATE_FILES:
                source = self.home / rel
                if not source.is_file() or rel == omit:
                    continue
                target = snap / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                files[rel] = target.stat().st_size
            (snap / "manifest.json").write_text(json.dumps({"files": files, "label": "scheduled"}))
            return snap.name
        native.create_quick_snapshot = create
        return native

    def test_quick_snapshot_preserves_native_manifest_for_restore(self):
        self.put(self.home, "config.yaml")
        self.put(self.home, "profiles/custom/memories/USER.md", "preferences")
        native = self.native()
        snapshot = self.module.quick_snapshot(self.home, native)
        self.assertEqual(snapshot.parent, self.home / "state-snapshots")
        manifest = json.loads((snapshot / "manifest.json").read_text())
        self.assertIn("profiles/custom/memories/USER.md", manifest["files"])
        self.assertEqual(native._QUICK_STATE_FILES, ("config.yaml",))
        self.assertEqual(native._QUICK_SNAPSHOTS_DIR, "state-snapshots")
        # Native restore iterates manifest['files'], not a hardcoded allow-list.
        restored = self.root / "restored"
        for rel in manifest["files"]:
            target = restored / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot / rel, target)
        self.assertEqual((restored / "profiles/custom/memories/USER.md").read_text(), "preferences")

    def test_incomplete_snapshot_does_not_publish_or_prune(self):
        self.put(self.home, "SOUL.md")
        old = self.put(self.home, "state-snapshots/old/manifest.json", "old backup")
        with self.assertRaisesRegex(RuntimeError, "missing"):
            self.module.quick_snapshot(self.home, self.native(omit="SOUL.md"))
        self.assertEqual(old.read_text(), "old backup")
        self.assertFalse((old.parent.parent / "test-scheduled").exists())

    def test_full_archive_must_include_all_existing_profiles(self):
        self.put(self.home, "profiles/custom/SOUL.md")
        expected = self.module.personal_files(self.home)
        archive = self.root / "full.zip"
        with zipfile.ZipFile(archive, "w") as backup:
            backup.writestr("config.yaml", "{}")
        with self.assertRaisesRegex(RuntimeError, "missing"):
            self.module.verify_full(archive, expected)
        with zipfile.ZipFile(archive, "a") as backup:
            backup.writestr("profiles/custom/SOUL.md", "personal text")
        self.module.verify_full(archive, expected)

    def test_scheduled_shell_checks_full_archive_before_publishing_and_pruning(self):
        self.put(self.home, "SOUL.md")
        self.put(self.workspace, "project/AGENTS.extra.md")
        repo = Path(__file__).resolve().parents[1]
        installed = self.root / "installed"
        installed.mkdir()
        for path in [repo / "ops/backup.sh", repo / "runtime/backup-personal-state.py",
                     repo / "runtime/manage-workspace-agents.py"]:
            shutil.copy2(path, installed / path.name)
        binaries = self.root / "bin"
        flock = self.put(binaries, "flock", "#!/bin/sh\nexit 0\n")
        flock.chmod(0o700)
        interpreter = self.home / "hermes-agent/venv/bin/python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        cli = self.put(binaries, "hermes", f"#!{sys.executable}\n" + '''
import os, pathlib, sys, zipfile
home = pathlib.Path(os.environ["HERMES_HOME"])
with zipfile.ZipFile(sys.argv[sys.argv.index("--output") + 1], "w") as archive:
    for name in ["SOUL.md", "operator-state/workspace-instructions.json"]:
        if name == "SOUL.md" and (home / "omit").exists():
            continue
        archive.write(home / name, name)
''')
        cli.chmod(0o700)
        pruner = self.put(installed, "prune-backups.py", '''
import os, pathlib
pathlib.Path(os.environ["HERMES_HOME"], "pruned").touch()
''')
        backups = self.root / "backups"
        env = {**os.environ, "HOME": str(self.root), "HERMES_HOME": str(self.home),
               "HERMES_WORKSPACE": str(self.workspace), "HERMES_BIN": str(cli),
               "HERMES_BACKUP_DIR": str(backups), "HERMES_RUN_AS_USER": "",
               "HERMES_BACKUP_PRUNER": str(pruner), "PATH": f"{binaries}:{os.environ['PATH']}"}
        self.put(self.home, "omit")
        result = subprocess.run(["bash", str(installed / "backup.sh")], env=env,
                                text=True, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Full backup missing", result.stderr)
        self.assertFalse(list(backups.glob("*.zip")))
        self.assertFalse((self.home / "pruned").exists())
        (self.home / "omit").unlink()
        result = subprocess.run(["bash", str(installed / "backup.sh")], env=env,
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        archives = list(backups.glob("scheduled-full-*.zip"))
        self.assertEqual(len(archives), 1)
        self.assertTrue((self.home / "pruned").exists())
        self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

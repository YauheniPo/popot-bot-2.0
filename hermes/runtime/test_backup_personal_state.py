"""Personal state must round-trip without broadening filesystem access."""

import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import types
from types import SimpleNamespace
import unittest
from unittest import mock
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
        # main() tightens the process-wide umask; restore it so a test that
        # calls main() cannot change how later tests create files.
        self.addCleanup(os.umask, os.umask(0o022))

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
        # Building the native stub is setup; only the snapshot call must raise.
        native = self.native(omit="SOUL.md")
        with self.assertRaisesRegex(RuntimeError, "missing"):
            self.module.quick_snapshot(self.home, native)
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

    def test_full_backup_crc_failure_is_rejected(self):
        archive = self.root / "corrupt.zip"
        with zipfile.ZipFile(archive, "w") as backup:
            backup.writestr("config.yaml", "{}")
        with zipfile.ZipFile(archive) as backup:
            raw = archive.read_bytes()
        # Flip bytes inside the stored payload so the CRC no longer matches.
        payload = b"config.yaml{}"
        position = raw.find(payload)
        self.assertGreaterEqual(position, 0)
        corrupted = bytearray(raw)
        corrupted[position + len(payload) - 1] ^= 0xFF
        archive.write_bytes(bytes(corrupted))
        with self.assertRaisesRegex(RuntimeError, "CRC"):
            self.module.verify_full(archive, ["config.yaml"])

    def test_backup_directory_symlink_and_walk_error_are_rejected(self):
        real = self.home / "real-backups"
        real.mkdir()
        self.put(real, "one.txt")
        link = self.home / "linked-backups"
        link.symlink_to(real)
        with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
            list(self.module.files_under(link))
        self.put(real, "two.txt")
        self.assertEqual(len(list(self.module.files_under(real))), 2)

    def test_unreadable_directory_aborts_the_walk(self):
        real = self.home / "private"
        real.mkdir()
        self.put(real, "one.txt")
        real.chmod(0o000)
        self.addCleanup(real.chmod, 0o755)
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory permissions")
        with self.assertRaises(OSError):
            list(self.module.files_under(real))

    def test_symlinked_profiles_directory_is_rejected(self):
        outside = self.root / "outside-profiles"
        outside.mkdir()
        (self.home / "profiles").symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "Profile directory must not be a symlink"):
            self.module.personal_files(self.home)

    def test_quick_snapshot_expands_a_listed_directory_entry(self):
        self.put(self.home, "config.yaml", "model: fixture\n")
        nested = self.put(self.home, "profiles/custom/memories/USER.md", "preferences")
        native = SimpleNamespace(_QUICK_STATE_FILES=("config.yaml", "profiles"),
                                 _QUICK_SNAPSHOTS_DIR="state-snapshots")

        def create(**kwargs):
            snap = self.home / "ops/state-snapshots/expanded"
            snap.mkdir(parents=True)
            files = {}
            for rel in native._QUICK_STATE_FILES:
                source = self.home / rel
                targets = ([source] if source.is_file()
                           else [p for p in source.rglob("*") if p.is_file()])
                for item in targets:
                    relname = item.relative_to(self.home).as_posix()
                    target = snap / relname
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target)
                    files[relname] = target.stat().st_size
            (snap / "manifest.json").write_text(json.dumps({"files": files}))
            return snap.name

        native.create_quick_snapshot = create
        destination = self.module.quick_snapshot(self.home, native)
        # The directory entry is expanded to its regular files, so the nested
        # memory file is verified rather than the directory itself.
        self.assertTrue((destination / "profiles/custom/memories/USER.md").is_file())
        self.assertEqual((destination / "profiles/custom/memories/USER.md").read_text(),
                         nested.read_text())

    def test_quick_snapshot_rejects_a_native_incomplete_backup_report(self):
        self.put(self.home, "config.yaml", "model: fixture\n")

        def create(**kwargs):
            snap = self.home / "ops/state-snapshots/reported"
            snap.mkdir(parents=True)
            target = snap / "config.yaml"
            shutil.copy2(self.home / "config.yaml", target)
            (snap / "manifest.json").write_text(json.dumps(
                {"files": {"config.yaml": target.stat().st_size}, "oversized_skipped": ["state.db"]}))
            return snap.name

        native = SimpleNamespace(_QUICK_STATE_FILES=("config.yaml",),
                                 _QUICK_SNAPSHOTS_DIR="state-snapshots",
                                 create_quick_snapshot=create)
        with self.assertRaisesRegex(RuntimeError, "incomplete quick backup"):
            self.module.quick_snapshot(self.home, native)

    def test_quick_snapshot_rejects_an_unusable_native_result(self):
        self.put(self.home, "config.yaml")
        for snapshot_id, message in (("", "valid quick snapshot"),
                                     ("../escape", "valid quick snapshot")):
            native = self.native()
            native.create_quick_snapshot = lambda _id=snapshot_id, **_: _id
            with self.subTest(snapshot_id=snapshot_id), self.assertRaisesRegex(RuntimeError, message):
                self.module.quick_snapshot(self.home, native)

    def test_quick_snapshot_rejects_an_incomplete_manifest(self):
        self.put(self.home, "config.yaml")
        self.put(self.home, "SOUL.md", "personal text")

        def create(**kwargs):
            snap = self.home / "ops/state-snapshots/reported"
            snap.mkdir(parents=True)
            (snap / "manifest.json").write_text(json.dumps(
                {"files": {"config.yaml": 1}, "failed_dbs": ["state.db"]}))
            return snap.name

        native = SimpleNamespace(_QUICK_STATE_FILES=("config.yaml",),
                                 _QUICK_SNAPSHOTS_DIR="state-snapshots",
                                 create_quick_snapshot=create)
        with self.assertRaisesRegex(RuntimeError, "missing required state files"):
            self.module.quick_snapshot(self.home, native)

        def create_incomplete(**kwargs):
            snap = self.home / "ops/state-snapshots/reported"
            snap.mkdir(parents=True)
            (snap / "manifest.json").write_text(json.dumps(
                {"files": {}, "failed_dbs": ["state.db"]}))
            return snap.name

        native.create_quick_snapshot = create_incomplete
        with self.assertRaisesRegex(RuntimeError, "missing required state files"):
            self.module.quick_snapshot(self.home, native)

    def test_quick_snapshot_rejects_a_size_mismatch(self):
        self.put(self.home, "config.yaml", "current")

        def create_with_truncated_file(**kwargs):
            snap = self.home / "ops/state-snapshots/mismatched"
            snap.mkdir(parents=True)
            (snap / "config.yaml").write_text("x")
            (snap / "manifest.json").write_text(json.dumps({"files": {"config.yaml": 999}}))
            return snap.name

        native = SimpleNamespace(_QUICK_STATE_FILES=("config.yaml",),
                                 _QUICK_SNAPSHOTS_DIR="state-snapshots",
                                 create_quick_snapshot=create_with_truncated_file)
        with self.assertRaisesRegex(RuntimeError, "file verification failed"):
            self.module.quick_snapshot(self.home, native)

    def test_quick_snapshot_rejects_an_occupied_destination(self):
        self.put(self.home, "config.yaml", "current")
        snapshot_id = "test-scheduled"

        def create(**kwargs):
            snap = self.home / "ops/state-snapshots" / snapshot_id
            snap.mkdir(parents=True)
            target = snap / "config.yaml"
            shutil.copy2(self.home / "config.yaml", target)
            (snap / "manifest.json").write_text(json.dumps(
                {"files": {"config.yaml": target.stat().st_size}}))
            return snapshot_id

        native = SimpleNamespace(_QUICK_STATE_FILES=("config.yaml",),
                                 _QUICK_SNAPSHOTS_DIR="state-snapshots",
                                 create_quick_snapshot=create)
        (self.home / "state-snapshots" / snapshot_id).mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "destination already exists"):
            self.module.quick_snapshot(self.home, native)

    def test_quick_snapshot_reports_an_empty_home(self):
        native = SimpleNamespace(_QUICK_STATE_FILES=(), _QUICK_SNAPSHOTS_DIR="state-snapshots")
        with self.assertRaisesRegex(RuntimeError, "No Hermes state found"):
            self.module.quick_snapshot(self.home, native)

    def test_quick_snapshot_expands_directory_entries_and_restores_native_settings(self):
        self.put(self.home, "state.db")
        nested = self.put(self.home, "config.yaml", "content")
        native = SimpleNamespace(_QUICK_STATE_FILES=("config.yaml",),
                                 _QUICK_SNAPSHOTS_DIR="state-snapshots")
        seen = {}

        def create(**kwargs):
            snap = self.home / "ops/state-snapshots/built"
            snap.mkdir(parents=True)
            files = {}
            for rel in native._QUICK_STATE_FILES:
                target = snap / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.home / rel, target)
                files[rel] = target.stat().st_size
            (snap / "manifest.json").write_text(json.dumps({"files": files}))
            seen["files"] = native._QUICK_STATE_FILES
            return snap.name

        native.create_quick_snapshot = create
        destination = self.module.quick_snapshot(self.home, native)
        self.assertIn("config.yaml", seen["files"])
        self.assertEqual(native._QUICK_STATE_FILES, ("config.yaml",))
        self.assertEqual(native._QUICK_SNAPSHOTS_DIR, "state-snapshots")
        self.assertTrue((destination / nested.name).is_file())

    def test_manifest_entries_must_be_safe_and_typed(self):
        for manifest in ({"version": 1, "files": []},
                         {"version": 2, "files": {}},
                         {"version": 1, "files": {"/abs/AGENTS.md": "x"}},
                         {"version": 1, "files": {"../AGENTS.md": "x"}},
                         {"version": 1, "files": {"AGENTS.md": 5}}):
            # Writing the manifest is setup; only the restore below must raise.
            self.module.instruction_io.write_atomic(
                self.home / self.module.MANIFEST, json.dumps(manifest))
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                self.module.restore_workspace(self.home, self.workspace)

    def test_instruction_read_failure_during_backup_is_reported(self):
        self.put(self.workspace, "AGENTS.md")
        with mock.patch.object(self.module.instruction_io, "read_optional", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "changed during backup"):
                self.module.mirror_workspace(self.home, self.workspace)

    def test_profile_credentials_must_not_link_outside_the_shared_path(self):
        profile = self.home / "profiles/custom"
        profile.mkdir(parents=True)
        outside = self.put(self.root, "outside.env")
        (profile / ".env").symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "outside the shared Hermes credential path"):
            self.module.personal_files(self.home)

    def test_main_mirrors_and_restores_workspace_instructions(self):
        self.put(self.workspace, "project/AGENTS.extra.md", "project rules")
        for command in ("mirror", "restore"):
            with self.subTest(command=command), \
                 mock.patch.object(self.module.sys, "argv",
                                   ["backup", command, "--hermes-home", str(self.home),
                                    "--workspace", str(self.workspace)]), \
                 mock.patch.object(self.module.sys, "stdout", io.StringIO()) as out:
                self.assertEqual(self.module.main(), 0)
            if command == "mirror":
                self.assertIn("project/AGENTS.extra.md", (self.home / self.module.MANIFEST).read_text())
            else:
                self.assertIn("Restored 1 workspace instruction file(s)", out.getvalue())

    def test_main_reports_a_legacy_archive_without_a_manifest(self):
        archive = self.root / "legacy.zip"
        with zipfile.ZipFile(archive, "w") as backup:
            backup.writestr("unrelated.txt", "legacy")
        with mock.patch.object(self.module.sys, "argv",
                               ["backup", "restore", "--hermes-home", str(self.home),
                                "--workspace", str(self.workspace), "--archive", str(archive)]), \
             mock.patch.object(self.module.sys, "stdout", io.StringIO()) as out:
            self.assertEqual(self.module.main(), 0)
        self.assertIn("No workspace instruction manifest (legacy backup)", out.getvalue())

    def test_main_requires_the_arguments_each_command_needs(self):
        for argv in (["backup", "mirror", "--hermes-home", str(self.home)],
                     ["backup", "verify-full", "--hermes-home", str(self.home)]):
            with self.subTest(argv=argv), \
                 mock.patch.object(self.module.sys, "argv", argv), \
                 mock.patch.object(self.module.sys, "stderr", io.StringIO()), \
                 self.assertRaises(SystemExit):
                self.module.main()

    def test_main_writes_the_inventory_and_verifies_a_full_archive(self):
        self.put(self.home, "SOUL.md", "identity")
        expected = self.module.personal_files(self.home)
        archive = self.root / "full.zip"
        with zipfile.ZipFile(archive, "w") as backup:
            for name in expected:
                backup.write(self.home / name, name)
        with mock.patch.object(self.module.sys, "argv",
                               ["backup", "inventory", "--hermes-home", str(self.home)]):
            self.assertEqual(self.module.main(), 0)
        self.assertEqual(json.loads((self.home / self.module.INVENTORY).read_text()), expected)
        with mock.patch.object(self.module.sys, "argv",
                               ["backup", "verify-full", "--hermes-home", str(self.home),
                                "--archive", str(archive)]):
            self.assertEqual(self.module.main(), 0)

    def test_main_quick_command_delegates_to_the_installed_backup_module(self):
        self.put(self.home, "config.yaml")
        # main() imports the pinned Hermes backup module from the installed
        # tree; supply the same fixture so the CLI branch runs without Hermes.
        stub = self.native()
        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.backup = stub
        with mock.patch.object(self.module.sys, "argv",
                               ["backup", "quick", "--hermes-home", str(self.home)]), \
             mock.patch.dict(self.module.sys.modules,
                             {"hermes_cli": hermes_cli, "hermes_cli.backup": stub}), \
             mock.patch.object(self.module.sys, "stdout", io.StringIO()) as out:
            self.assertEqual(self.module.main(), 0)
        self.assertIn("Verified scheduled quick backup: test-scheduled", out.getvalue())
        self.assertTrue((self.home / "state-snapshots/test-scheduled").is_dir())


    def test_module_entrypoint_returns_a_status(self):
        # The module-level guard is only reachable via runpy; invoke the script
        # as __main__ with a bad argument so no backup is attempted.
        script = Path(__file__).with_name("backup-personal-state.py")
        with mock.patch.object(sys, "argv", ["backup-personal-state.py"]):
            with self.assertRaises(SystemExit) as exit_code:
                runpy.run_path(str(script), run_name="__main__")
        self.assertEqual(exit_code.exception.code, 2)


if __name__ == '__main__':
    unittest.main()

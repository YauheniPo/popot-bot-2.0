"""Attached cron skills keep read tools under a restricted per-job allowlist."""

import copy
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "cron_skill_patches", Path(__file__).with_name("apply-hermes-patches.py"))
patches = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patches)

# Minimal native resolver contract from the pinned cron/scheduler.py. Network-
# free regression; test_hermes_upstream also runs against the actual checkout.
SCHEDULER_SOURCE = '''def _resolve_cron_enabled_toolsets(job: dict, cfg: dict) -> list[str]:
    per_job = job.get("enabled_toolsets")
    if per_job:
        return _merge_mcp_into_per_job_toolsets(list(per_job), cfg or {})
    try:
        from hermes_cli.tools_config import _get_platform_tools  # lazy: avoid heavy import at cron module load
        return sorted(_get_platform_tools(cfg or {}, "cron"))
    except Exception as exc:
        raise RuntimeError("Cron toolset resolution failed") from exc
'''
TOOLSETS_SOURCE = '''def _ts(description, tools=(), includes=(), **extra):
    return {"description": description, "tools": list(tools), "includes": list(includes), **extra}
TOOLSETS = {
    "terminal": _ts("Terminal", ["terminal", "process_manage"]),
    "web": _ts("Web", ["web_search", "web_extract"]),
    "file": _ts("Files", ["read_file", "write_file", "patch", "search_files"]),
    "skills": _ts(
        "Skills", ["skills_list", "skill_view", "skill_manage"]),
}
'''


def cron_patches():
    return [patch for patch in patches._PATCHES
            if patch[1].startswith("# Local Hermes: cron skill")]


def apply_source(path, source):
    for target, _marker, old, new in cron_patches():
        if target == path:
            if source.count(old) != 1:
                raise AssertionError(f"Unmatched/ambiguous cron patch: {target}")
            source = source.replace(old, new, 1)
    return source


class CronSkillToolsTests(unittest.TestCase):
    def setUp(self):
        self.config = {"platform_toolsets": {"cron": ["web", "file"]}}
        tools_config = SimpleNamespace(_get_platform_tools=lambda cfg, platform:
                                      cfg["platform_toolsets"][platform])
        self.modules = mock.patch.dict(sys.modules, {"hermes_cli.tools_config": tools_config})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.merge = mock.Mock(side_effect=lambda tools, cfg: list(tools))
        namespace = {"_merge_mcp_into_per_job_toolsets": self.merge}
        exec(apply_source("cron/scheduler.py", SCHEDULER_SOURCE), namespace)
        self.resolve = namespace["_resolve_cron_enabled_toolsets"]
        toolsets = {}
        exec(apply_source("toolsets.py", TOOLSETS_SOURCE), toolsets)
        self.toolsets = toolsets["TOOLSETS"]

    def tools(self, job, disabled=()):
        enabled = self.resolve(job, self.config)
        names = {tool for name in enabled for tool in self.toolsets[name]["tools"]}
        for name in disabled:
            names.difference_update(self.toolsets[name]["tools"])
        return names

    def test_monitor_can_load_skill_and_research_without_skill_management(self):
        job = {"id": "existing", "skills": ["competitor-news-monitor"],
               "enabled_toolsets": ["terminal", "web", "file"], "no_agent": False,
               "schedule": {"cron": "0 9 * * 5"}, "deliver": "telegram:fixture",
               "repeat": {"times": None}, "model": "personal-pin",
               "last_run_record": "previous.json", "last_cutoff": "unchanged"}
        before = copy.deepcopy(job)
        names = self.tools(job)
        self.assertTrue({"skill_view", "skills_list", "web_search", "web_extract",
                         "read_file", "write_file", "terminal"}.issubset(names))
        self.assertNotIn("skill_manage", names)
        self.assertNotIn("cronjob_manage", names)
        self.assertEqual(job, before)
        self.assertEqual(self.config, {"platform_toolsets": {"cron": ["web", "file"]}})
        self.merge.assert_called_once_with(before["enabled_toolsets"], self.config)

    def test_attached_skill_works_with_platform_toolsets_and_legacy_skill(self):
        for attachment in ({"skills": ["monitor"]}, {"skills": "monitor"}, {"skill": "monitor"}):
            with self.subTest(attachment=attachment):
                self.assertIn("skill_view", self.tools(attachment))
                self.assertNotIn("skill_manage", self.tools(attachment))

    def test_jobs_without_skill_or_with_no_agent_keep_their_tools(self):
        for attachment in ({}, {"skills": []}, {"skills": "  "},
                           {"skills": [], "skill": "ignored"},
                           {"skills": ["monitor"], "no_agent": True}):
            with self.subTest(attachment=attachment):
                job = {"enabled_toolsets": ["web", "file"], **attachment}
                self.assertEqual(self.resolve(job, self.config), ["web", "file"])

    def test_explicit_skills_access_is_preserved_and_not_duplicated(self):
        job = {"skills": ["monitor"], "enabled_toolsets": ["web", "skills"]}
        self.assertEqual(self.resolve(job, self.config), ["web", "skills"])
        self.assertIn("skill_manage", self.tools(job))

    def test_empty_platform_selection_stays_empty(self):
        self.config["platform_toolsets"]["cron"] = []
        self.assertEqual(self.resolve({"skills": ["monitor"]}, self.config), [])

    def test_cron_readers_are_not_duplicated(self):
        job = {"skills": ["monitor"], "enabled_toolsets": ["web", "cron-skills"]}
        self.assertEqual(self.resolve(job, self.config), ["web", "cron-skills"])

    def test_global_skills_deny_removes_auto_added_readers(self):
        job = {"skills": ["monitor"], "enabled_toolsets": ["web", "file"]}
        names = self.tools(job, disabled=["skills"])
        self.assertNotIn("skill_view", names)
        self.assertNotIn("skills_list", names)
        self.assertIn("web_search", names)

    def test_resolution_errors_fail_closed(self):
        with mock.patch.object(sys.modules["hermes_cli.tools_config"], "_get_platform_tools",
                               side_effect=ValueError("bad config")):
            with self.assertRaisesRegex(RuntimeError, "Cron toolset resolution failed"):
                self.resolve({"skills": ["monitor"]}, self.config)

    def test_patches_repeat_reapply_after_source_update_and_reject_changed_anchor(self):
        required = cron_patches()
        self.assertTrue(required, "Cron skill repair must be registered in the managed updater")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            originals = {"cron/scheduler.py": SCHEDULER_SOURCE, "toolsets.py": TOOLSETS_SOURCE}
            for name, source in originals.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source)
            state = {}
            with mock.patch.object(patches, "HERMES_AGENT_DIR", root), \
                    mock.patch("sys.stdout", new_callable=io.StringIO):
                for patch in required:
                    self.assertEqual(patches._apply_one_patch(*patch, state)[:2], (1, None))
                first = {name: (root / name).read_bytes() for name in originals}
                for patch in required:
                    self.assertEqual(patches._apply_one_patch(*patch, state), (0, None, False))
                self.assertEqual({name: (root / name).read_bytes() for name in originals}, first)
                # Upstream checkout replaces local source; persisted fingerprints survive.
                for name, source in originals.items():
                    (root / name).write_text(source)
                for patch in required:
                    self.assertEqual(patches._apply_one_patch(*patch, state)[:2], (1, None))
                self.assertEqual({name: (root / name).read_bytes() for name in originals}, first)
                target = root / "cron/scheduler.py"
                target.write_text("# incompatible upstream\n")
                patch = next(item for item in required if item[0] == "cron/scheduler.py")
                with mock.patch("sys.stderr", new_callable=io.StringIO):
                    self.assertEqual(patches._apply_one_patch(*patch, state)[:2], (0, "cron/scheduler.py"))
                self.assertEqual(target.read_text(), "# incompatible upstream\n")


if __name__ == "__main__":
    unittest.main()

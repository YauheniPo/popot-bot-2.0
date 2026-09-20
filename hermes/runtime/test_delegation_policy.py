"""Contracts for Telegram delegation policy and its deployed instructions."""

import importlib.util
from pathlib import Path
import unittest

import yaml
from jinja2 import Environment, StrictUndefined


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("delegation_apply_config", ROOT / "runtime/apply-config.py")
planner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(planner)


class DelegationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.settings = yaml.safe_load((ROOT / "config/vps-defaults.yml").read_text())

    def test_delegation_has_bounded_budgets_without_automatic_approval(self):
        runtime = self.settings["vps_runtime"]["set"]
        for key in ("max_concurrent_children", "max_iterations", "child_timeout_seconds"):
            value = runtime[f"delegation.{key}"]
            self.assertIs(type(value), int)
            self.assertGreater(value, 0)
        self.assertGreaterEqual(runtime["delegation.child_timeout_seconds"], 30)
        self.assertEqual(runtime["delegation.max_spawn_depth"], 1)
        self.assertIs(runtime["delegation.subagent_auto_approve"], False)

    def test_team_policy_has_bounded_budget_and_explicit_plugin_sources(self):
        policy = self.settings['vps_hermes']['config']['managed_overlay']['team_workflow']
        self.assertIsInstance(policy['enabled'], bool)
        self.assertIs(type(policy['max_revision_rounds']), int)
        self.assertGreaterEqual(policy['max_revision_rounds'], 0)
        self.assertLessEqual(policy['max_revision_rounds'], 5)
        self.assertGreaterEqual(policy['deadline_seconds'], 30)
        self.assertLessEqual(policy['deadline_seconds'], 7200)
        tasks = yaml.safe_load((ROOT / 'ansible/tasks/runtime.yml').read_text())
        task = next(task for task in tasks if task['name'] == 'Install the managed native team workflow plugin')
        self.assertEqual(set(task['loop']), {'__init__.py', 'engine.py', 'plugin.yaml'})
        self.assertEqual(task['notify'], 'restart Hermes gateway')
        for name in task['loop']:
            self.assertTrue((ROOT / 'ops/plugin/team-workflow' / name).is_file())

    def test_deploy_enforces_delegation_policy_even_with_workspace_enabled(self):
        current = {"delegation": {"max_concurrent_children": 999,
                                  "subagent_auto_approve": True}}
        operations = planner.build_operations(self.settings, current,
                                             {"HERMES_WORKSPACE": "/tmp/work"}, set())
        expected = {key: value for key, value in self.settings["vps_runtime"]["set"].items()
                    if key.startswith("delegation.")}
        self.assertTrue(expected)
        actual = {op.key: op.value for op in operations if op.action == "set"}
        for key, value in expected.items():
            self.assertEqual(actual[key], value)

    def test_instructions_render_tuned_budgets_without_hardcoded_defaults(self):
        template = Environment(undefined=StrictUndefined).from_string(
            (ROOT / "ansible/templates/delegation-policy.md.j2").read_text())
        runtime = self.settings["vps_runtime"]["set"]
        for count, iterations, seconds in ((2, 40, 900), (3, 60, 1200)):
            tuned = {**runtime, "delegation.max_concurrent_children": count,
                     "delegation.max_iterations": iterations,
                     "delegation.child_timeout_seconds": seconds}
            rendered = template.render(vps_runtime={"set": tuned})
            for value in (count, iterations, seconds):
                self.assertIn(str(value), rendered)
            for role in ("Researcher", "Developer", "Reviewer", "VPS/CI diagnostician"):
                self.assertIn(role, rendered)
            for contract in ("delegate_task", "action=\"list\"", "action=\"stop\"",
                             "OpenRouter", ":free", "Telegram", "completion"):
                self.assertIn(contract, rendered)

    def test_policy_is_published_privately_before_backup_independently_of_host_admin(self):
        tasks = yaml.safe_load((ROOT / "ansible/playbook.yml").read_text())[0]["tasks"]
        def tasks_in(block):
            for task in block:
                yield task
                yield from tasks_in(task.get("block", []))
        tasks = list(tasks_in(tasks))
        indices = [i for i, task in enumerate(tasks) if "MANAGED DELEGATION POLICY" in
                   task.get("ansible.builtin.blockinfile", {}).get("marker", "")]
        self.assertEqual(len(indices), 1)
        task = tasks[indices[0]]
        options = task["ansible.builtin.blockinfile"]
        self.assertNotIn("when", task)
        self.assertEqual(options["path"], "{{ hermes_workspace }}/AGENTS.md")
        self.assertEqual(options["mode"], "0600")
        self.assertIn("templates/delegation-policy.md.j2", options["block"])
        backup = next(i for i, task in enumerate(tasks) if task["name"] ==
                      "Mirror the final workspace instructions into Hermes backup state")
        self.assertLess(indices[0], backup)


if __name__ == "__main__":
    unittest.main()

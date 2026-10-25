"""Tests for the Hermes patch-layer audit tool."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("audit-hermes-patches.py")
SPEC = importlib.util.spec_from_file_location("audit_hermes_patches", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
audit_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit_module
SPEC.loader.exec_module(audit_module)


class ClassifyBlockTests(unittest.TestCase):
    def test_unpatched_upstream_is_applicable(self) -> None:
        source = "def load(agent):\n    return agent.chain\n"
        self.assertEqual(
            audit_module.classify_block("    return agent.chain\n", "    return filter(agent.chain)\n", source),
            "APPLICABLE",
        )

    def test_upstream_containing_the_replacement_is_obsolete(self) -> None:
        source = '        "model": agent.model,\n        "provider": agent.provider,\n'
        self.assertEqual(
            audit_module.classify_block('        "model": agent.model,\n', '        "provider": agent.provider,\n', source),
            "OBSOLETE",
        )

    def test_moved_code_is_drift(self) -> None:
        source = "def load(agent):\n    return apply(agent)\n"
        self.assertEqual(
            audit_module.classify_block("    return agent.chain\n", "    return filter(agent.chain)\n", source),
            "DRIFT",
        )

    def test_missing_upstream_file_is_reported(self) -> None:
        self.assertEqual(audit_module.classify_block("old\n", "new\n", ""), "NO_FILE")

    def test_blank_replacement_never_counts_as_obsolete(self) -> None:
        source = "    return agent.chain\n"
        self.assertEqual(audit_module.classify_block("    return agent.chain\n", "   ", source), "APPLICABLE")


class PatchLayerTests(unittest.TestCase):
    def test_every_block_has_a_marker_and_target(self) -> None:
        applier = MODULE_PATH.with_name("apply-hermes-patches.py")
        blocks = audit_module.load_blocks(applier)
        self.assertGreater(len(blocks), 0)
        for block in blocks:
            self.assertEqual(len(block), 4, block)
            relative_path, label, old, new = block
            self.assertTrue(relative_path.endswith(".py"), relative_path)
            self.assertIn("# Local Hermes:", label)
            self.assertTrue(old.strip(), label)
            self.assertTrue(new.strip(), label)

    def test_summary_counts_every_verdict(self) -> None:
        rows = [
            {"n": 1, "path": "a.py", "label": "l", "verdict": "APPLICABLE"},
            {"n": 2, "path": "b.py", "label": "l", "verdict": "DRIFT"},
        ]
        self.assertEqual(
            audit_module.summarize(rows),
            {"APPLICABLE": 1, "OBSOLETE": 0, "DRIFT": 1, "NO_FILE": 0},
        )


if __name__ == "__main__":
    unittest.main()

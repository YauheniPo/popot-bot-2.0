"""Structural checks for the versioned Prometheus rules and dashboard queries."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import yaml

OBSERVABILITY = Path(__file__).resolve().parent
RULES = OBSERVABILITY / "rules" / "hermes.rules.yml"
DASHBOARD = OBSERVABILITY / "grafana" / "provisioning" / "dashboards" / "hermes" / "hermes-overview.json"
EXPORTER = OBSERVABILITY.parent / "ops" / "export-metrics.py"
METRIC_NAME = re.compile(r"\bhermes_[a-z0-9_:]+")


def exported_metric_names() -> set[str]:
    names = set(METRIC_NAME.findall(EXPORTER.read_text(encoding="utf-8")))
    # The histogram is emitted as its three Prometheus series.
    names |= {f"{name}_{suffix}" for name in names if name.endswith("_ms") for suffix in ("bucket", "sum", "count")}
    return names


class PrometheusRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.groups = yaml.safe_load(RULES.read_text(encoding="utf-8"))["groups"]
        self.rules = [rule for group in self.groups for rule in group["rules"]]

    def test_every_rule_records_or_alerts_with_an_expression(self) -> None:
        self.assertTrue(self.rules)
        for rule in self.rules:
            self.assertEqual(len([k for k in rule if k in ("record", "alert")]), 1, rule)
            self.assertIsInstance(rule.get("expr"), str, rule)
            self.assertTrue(rule["expr"].strip(), rule)
            if "alert" in rule:
                self.assertIn(rule["labels"]["severity"], {"info", "warning", "critical"}, rule)
                self.assertIn("summary", rule["annotations"], rule)

    def test_recorded_names_follow_prometheus_convention_and_are_unique(self) -> None:
        recorded = [rule["record"] for rule in self.rules if "record" in rule]
        self.assertEqual(len(recorded), len(set(recorded)))
        for name in recorded:
            self.assertRegex(name, r"^hermes_route:[a-z0-9_]+:[a-z0-9_]+$")

    def test_rules_and_dashboard_reference_only_known_metrics(self) -> None:
        recorded = {rule["record"] for rule in self.rules if "record" in rule}
        known = exported_metric_names() | recorded
        expressions = [rule["expr"] for rule in self.rules]
        panels = json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]
        expressions += [target["expr"] for panel in panels for target in panel.get("targets", [])]
        referenced = {name for expr in expressions for name in METRIC_NAME.findall(expr)}
        self.assertTrue(referenced)
        self.assertEqual(referenced - known, set(), "unknown metric names in rules or dashboard")

    def test_ratio_rules_guard_their_denominators(self) -> None:
        for rule in self.rules:
            if "record" in rule and ":ratio" in rule["record"]:
                self.assertIn("> 0)", rule["expr"], rule["record"])

    def test_prometheus_configs_load_the_rules_directory(self) -> None:
        for path, directory in ((OBSERVABILITY / "prometheus.yml", "/etc/prometheus/rules/*.yml"),
                                (OBSERVABILITY.parent / "ops" / "templates" / "hermes-prometheus.yml",
                                 "/etc/hermes-observability/rules/*.yml")):
            self.assertIn(directory, path.read_text(encoding="utf-8"), path)


if __name__ == "__main__":
    unittest.main()

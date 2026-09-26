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
        expressions += [target["expr"] for panel in panels for target in panel.get("targets", []) if "expr" in target]
        referenced = {name for expr in expressions for name in METRIC_NAME.findall(expr)}
        self.assertTrue(referenced)
        self.assertEqual(referenced - known, set(), "unknown metric names in rules or dashboard")

    def test_dashboard_has_clear_groups_and_no_duplicate_or_low_value_panels(self) -> None:
        panels = json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]
        titles = [panel["title"] for panel in panels]
        self.assertEqual(len(titles), len(set(titles)))
        self.assertNotIn("Profile last request", titles)
        self.assertIn("Profile last activity", titles)
        self.assertNotIn("Profile usage collection", titles)
        self.assertNotIn("LLM cost rate by model", titles)
        self.assertGreaterEqual(len([panel for panel in panels if panel["type"] == "row"]), 5)
        for panel in panels:
            with self.subTest(panel=panel["title"]):
                self.assertTrue(panel.get("description", "").strip())
                pos = panel["gridPos"]
                self.assertGreater(pos["h"], 0)
                self.assertGreater(pos["w"], 0)
                self.assertLessEqual(pos["x"] + pos["w"], 24)
        for i, left in enumerate(panels):
            a = left["gridPos"]
            for right in panels[i + 1:]:
                b = right["gridPos"]
                self.assertFalse(a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and
                                 a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"],
                                 f"overlap: {left['title']} / {right['title']}")

    def test_dashboard_rates_and_units_match_the_queries(self) -> None:
        panels = {p["title"]: p for p in json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]}
        failures = panels["Failed API requests"]
        latency = panels["Average model response time"]
        for panel in (failures, latency):
            self.assertNotIn("clamp_min", panel["targets"][0]["expr"])
            self.assertIn("rate(hermes_api_calls_total", panel["targets"][0]["expr"])
        self.assertEqual(failures["fieldConfig"]["defaults"]["unit"], "percentunit")
        self.assertEqual(latency["fieldConfig"]["defaults"]["unit"], "ms")
        self.assertIn("or vector(0)", failures["targets"][0]["expr"])
        self.assertNotIn("clamp_min", panels["Average model response time by route"]["targets"][0]["expr"])
        availability = panels["Successful API responses by model"]["targets"][0]["expr"]
        self.assertIn("clamp_min(", availability)
        self.assertIn("rate(hermes_api_rate_limits_total[$__rate_interval])",
                      panels["Rate-limited requests per second"]["targets"][0]["expr"])
        self.assertIn("Host load", panels)
        self.assertIn("No data means Prometheus returned no sample", panels["Gateway status"]["description"])
        self.assertIn("Inodes used", panels)
        self.assertIn("Gateway CPU use (cores)", panels)
        self.assertIn("Host CPU used", panels)
        self.assertEqual(panels["Inodes used"]["fieldConfig"]["defaults"]["unit"], "percentunit")
        self.assertEqual(panels["Host CPU used"]["fieldConfig"]["defaults"]["unit"], "percentunit")

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

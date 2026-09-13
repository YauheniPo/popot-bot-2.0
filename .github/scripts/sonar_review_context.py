#!/usr/bin/env python3
"""Fetch bounded SonarCloud findings for the current pull-request revision."""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request


SONAR_URL = "https://sonarcloud.io"
MAX_ISSUES = 100
MAX_MESSAGE = 600
OUTPUT_FILENAME = "sonar-review-context.json"


def _request(path: str, token: str, params: dict[str, str]) -> dict[str, object]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{SONAR_URL}{path}?{query}")
    request.add_header("Authorization", "Basic " + base64.b64encode(f"{token}:".encode()).decode())
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("SonarCloud returned an invalid response")
    return payload


def _changed_paths(base_sha: str, head_sha: str) -> set[str]:
    import subprocess

    result = subprocess.run(
        ["git", "diff", "--name-only", "-z", base_sha, head_sha, "--"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {path for path in result.stdout.split("\0") if path}


def build_context(project_key: str, pull_request: str, token: str, base_sha: str, head_sha: str) -> dict[str, object]:
    status = _request(
        "/api/qualitygates/project_status",
        token,
        {"projectKey": project_key, "pullRequest": pull_request},
    ).get("projectStatus", {})
    if not isinstance(status, dict):
        status = {}
    changed = _changed_paths(base_sha, head_sha)
    issue_payload = _request(
        "/api/issues/search",
        token,
        {
            "componentKeys": project_key,
            "pullRequest": pull_request,
            "statuses": "OPEN,CONFIRMED",
            "ps": str(MAX_ISSUES),
        },
    )
    issues: list[dict[str, object]] = []
    for issue in issue_payload.get("issues", []):
        if not isinstance(issue, dict):
            continue
        component = str(issue.get("component", ""))
        path = component.split(":", 1)[-1]
        if path not in changed:
            continue
        issues.append(
            {
                "key": issue.get("key"),
                "severity": issue.get("severity"),
                "type": issue.get("type"),
                "path": path,
                "line": issue.get("line"),
                "text_range": issue.get("textRange"),
                "message": str(issue.get("message", ""))[:MAX_MESSAGE],
            }
        )
    conditions = status.get("conditions", [])
    if not isinstance(conditions, list):
        conditions = []
    return {
        "source": "SonarCloud",
        "project": project_key,
        "pull_request": pull_request,
        "head_sha": head_sha,
        "quality_gate": status.get("status", "UNKNOWN"),
        "conditions": [
            {
                "metric": condition.get("metricKey"),
                "status": condition.get("status"),
                "actual": condition.get("actualValue"),
                "threshold": condition.get("errorThreshold"),
            }
            for condition in conditions
            if isinstance(condition, dict)
        ],
        "issues": issues,
        "issues_total": issue_payload.get("total", len(issues)),
    }


def main() -> int:
    token = os.environ.get("SONAR_TOKEN", "").strip()
    required = {name: os.environ.get(name, "").strip() for name in ("SONAR_PROJECT_KEY", "PR_NUMBER", "BASE_SHA", "HEAD_SHA")}
    if not token or not all(required.values()):
        raise RuntimeError("SONAR_TOKEN, SONAR_PROJECT_KEY, PR_NUMBER, BASE_SHA and HEAD_SHA are required")
    context = build_context(required["SONAR_PROJECT_KEY"], required["PR_NUMBER"], token, required["BASE_SHA"], required["HEAD_SHA"])
    with open(OUTPUT_FILENAME, "w", encoding="utf-8") as output_file:
        output_file.write(json.dumps(context, ensure_ascii=False, indent=2) + "\n")
    print(f"Sonar context saved: {OUTPUT_FILENAME} ({len(context['issues'])} changed-file issues)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Sonar review context unavailable: {error}")
        raise SystemExit(1)

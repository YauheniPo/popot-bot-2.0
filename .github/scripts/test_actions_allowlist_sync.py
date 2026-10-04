"""Exercise the no-checkout allowlist sync with mocked GitHub responses."""

import base64
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock
import urllib.error

import yaml


class ActionsAllowlistSyncTests(unittest.TestCase):
    old = "example/action@" + "1" * 40
    new = "example/action@" + "2" * 40

    def run_sync(self, refs, patterns=None, *, mirror_missing=False, truncated=False, token="test", fail_method=None):
        workflow = Path(__file__).parents[1] / "workflows/sync-actions-allowlist.yml"
        data = yaml.load(workflow.read_text(), Loader=yaml.BaseLoader)
        step = next(iter(data["jobs"].values()))["steps"][0]
        script = step["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        patterns = [self.old] if patterns is None else patterns
        calls = []

        def response(request, timeout):
            path = request.full_url.split("https://api.github.com", 1)[1]
            payload = json.loads(request.data) if request.data else None
            calls.append((request.method, path, payload))
            self.assertEqual(timeout, 20)
            if request.method == fail_method:
                raise urllib.error.HTTPError(request.full_url, 403, "denied", {}, io.BytesIO())
            if "/git/trees/" in path:
                document = {"truncated": truncated, "tree": [
                    {"type": "blob", "path": ".github/workflows/check.yml", "sha": "workflow"}
                ]}
            elif "/git/blobs/" in path:
                text = "jobs:\n  check:\n    steps:\n" + "".join(f"      - uses: {ref}\n" for ref in refs)
                document = {"content": base64.b64encode(text.encode()).decode()}
            elif request.method == "GET" and path.endswith("selected-actions"):
                document = {"patterns_allowed": list(patterns), "github_owned_allowed": False, "verified_allowed": False}
            elif request.method == "PATCH" and mirror_missing:
                raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, io.BytesIO())
            else:
                document = None
            result = mock.MagicMock()
            result.status = 204 if document is None else 200
            result.__enter__.return_value = result
            result.read.return_value = json.dumps(document).encode()
            return result

        output = io.StringIO()
        env = {"ACTIONS_POLICY_TOKEN": token, "REPOSITORY": "example/repo", "BASE_SHA": "before", "HEAD_SHA": "main"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("urllib.request.urlopen", side_effect=response), contextlib.redirect_stdout(output):
            try:
                exec(compile(script, str(workflow), "exec"), {})
            except SystemExit as error:
                code = error.code
            else:
                code = 0
        return code, calls, output.getvalue()

    def test_owner_merge_updates_policy_and_mirror_for_existing_source(self):
        code, calls, _ = self.run_sync([self.new])
        self.assertEqual(code, 0)
        writes = [(method, payload) for method, _, payload in calls if method != "GET"]
        self.assertEqual([method for method, _ in writes], ["PUT", "PATCH"])
        self.assertEqual(writes[0][1]["patterns_allowed"], [self.old, self.new])
        self.assertFalse(writes[0][1]["github_owned_allowed"])
        self.assertFalse(writes[0][1]["verified_allowed"])
        self.assertEqual(json.loads(writes[1][1]["value"]), [self.old, self.new])

    def test_already_allowed_revision_only_refreshes_mirror(self):
        code, calls, _ = self.run_sync([self.new], [self.new])
        self.assertEqual(code, 0)
        self.assertEqual([method for method, _, _ in calls if method != "GET"], ["PATCH"])

    def test_unapproved_source_or_unpinned_revision_stops_before_writes(self):
        for ref in ("other/action@" + "3" * 40, "example/action@v2", "example/action@main"):
            with self.subTest(ref=ref):
                code, calls, _ = self.run_sync([self.new, ref])
                self.assertEqual(code, 1)
                self.assertFalse(any(method != "GET" for method, _, _ in calls))

    def test_existing_wildcard_policy_is_preserved(self):
        code, calls, _ = self.run_sync([self.new], ["example/action@*"])
        self.assertEqual(code, 0)
        self.assertFalse(any(method == "PUT" for method, _, _ in calls))
        self.assertEqual(json.loads(calls[-1][2]["value"]), ["example/action@*"])

    def test_missing_mirror_is_created(self):
        code, calls, _ = self.run_sync([self.new], mirror_missing=True)
        self.assertEqual(code, 0)
        self.assertEqual([method for method, _, _ in calls if method != "GET"], ["PUT", "PATCH", "POST"])
        self.assertEqual(calls[-1][2]["name"], "ACTIONS_ALLOWED_PATTERNS")

    def test_api_permission_failure_is_not_treated_as_success(self):
        for method in ("PUT", "PATCH"):
            with self.subTest(method=method):
                with self.assertRaises(urllib.error.HTTPError):
                    self.run_sync([self.new], fail_method=method)

    def test_missing_token_fails_without_api_calls(self):
        code, calls, _ = self.run_sync([self.new], token="")
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])

    def test_truncated_tree_fails_without_writes(self):
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            self.run_sync([self.new], truncated=True)

    def test_manual_recovery_only_targets_main_without_external_actions(self):
        workflow = Path(__file__).parents[1] / "workflows/sync-actions-allowlist.yml"
        data = yaml.load(workflow.read_text(), Loader=yaml.BaseLoader)
        self.assertIn("workflow_dispatch", data["on"])
        self.assertEqual(data["concurrency"]["group"], "sync-actions-allowlist")
        self.assertEqual(data["concurrency"]["cancel-in-progress"], "false")
        job = next(iter(data["jobs"].values()))
        self.assertEqual(job["if"], "github.ref == 'refs/heads/main'")
        self.assertTrue(all("uses" not in step for step in job["steps"]))


if __name__ == "__main__":
    unittest.main()

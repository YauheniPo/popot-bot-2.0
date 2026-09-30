"""Exercise the actual no-checkout policy step without GitHub/network access."""

import base64
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

import yaml


class ActionPolicyValidationTests(unittest.TestCase):
    def run_validation(self, value, *, include_action=False):
        workflow = Path(__file__).parents[1] / "workflows/ai-review-action-policy.yml"
        data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        step = data["jobs"]["verify-claude-action-allowlist"]["steps"][0]
        script = step["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        env = {"REPOSITORY": "example/repo", "HEAD_SHA": "test", "GITHUB_TOKEN": "test",
               "GITHUB_STEP_SUMMARY": os.devnull}
        if value is not None:
            env["ACTIONS_ALLOWED_PATTERNS"] = value
        output = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen") as request, \
                contextlib.redirect_stdout(output):
            tree = {"tree": []}
            responses = [tree]
            if include_action:
                tree = {"tree": [{"type": "blob", "path": ".github/workflows/check.yml", "sha": "blob-sha"}]}
                content = "name: check\njobs:\n  test:\n    steps:\n      - uses: example/action@revision\n"
                responses = [tree, {"content": base64.b64encode(content.encode()).decode(), "encoding": "base64"}]
            request.side_effect = [self.api_response(item) for item in responses]
            try:
                exec(compile(script, str(workflow), "exec"), {})
            except SystemExit as error:
                code = error.code
            else:
                code = 0
        return code, output.getvalue(), request

    @staticmethod
    def api_response(document):
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(json.dumps(document).encode())
        return response

    def test_invalid_values_warn_and_continue_with_safe_diagnostic(self):
        for raw, detail in (("{}", "dict"), ('123', "int"), ('null', "NoneType"), ('[123]', "list")):
            with self.subTest(raw=raw):
                code, output, request = self.run_validation(raw)
                self.assertEqual(code, 0)
                # Assert the warning path still checks the tree with an empty allow-list.
                request.assert_called_once()
                sent = request.call_args.args[0]
                self.assertTrue(sent.full_url.endswith("/git/trees/test?recursive=1"))
                self.assertIn("all array items must be strings", output)
                self.assertIn(detail, output)
                self.assertNotIn("private-pattern", output)

    def test_invalid_json_or_missing_warns_and_continues_with_empty_allowlist(self):
        for raw, detail in ((None, "missing"), ("", "JSON"), ("{", "JSON")):
            with self.subTest(raw=raw):
                code, output, request = self.run_validation(raw)
                self.assertEqual(code, 0)
                # Invalid or missing configuration still checks the tree with no allowed actions.
                request.assert_called_once()
                sent = request.call_args.args[0]
                self.assertTrue(sent.full_url.endswith("/git/trees/test?recursive=1"))
                self.assertIn("using empty allow-list", output)
                self.assertIn(detail, output)
                self.assertNotIn("private-pattern", output)

    def test_arrays_of_strings_and_empty_deny_all_array_are_valid(self):
        for raw in ('[]', '["actions/checkout@*"]'):
            with self.subTest(raw=raw):
                code, _, request = self.run_validation(raw)
                self.assertEqual(code, 0)
                request.assert_called_once()

    def test_invalid_allowlist_continues_and_fails_closed_for_external_actions(self):
        code, output, request = self.run_validation("{}", include_action=True)
        self.assertEqual(code, 1)
        self.assertEqual(request.call_count, 2)
        self.assertIn("using empty allow-list", output)
        self.assertIn("Blocked CI Action revision", output)
        self.assertIn("example/action@revision", output)


if __name__ == "__main__":
    unittest.main()

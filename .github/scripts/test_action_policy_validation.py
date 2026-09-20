"""Exercise the actual no-checkout policy step without GitHub/network access."""

import contextlib
import io
import os
from pathlib import Path
import unittest
from unittest import mock

import yaml


class ActionPolicyValidationTests(unittest.TestCase):
    def run_validation(self, value):
        workflow = Path(__file__).parents[1] / "workflows/ai-review-action-policy.yml"
        data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        step = data["jobs"]["verify-claude-action-allowlist"]["steps"][0]
        script = step["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        env = {"REPOSITORY": "example/repo", "HEAD_SHA": "test", "GITHUB_TOKEN": "test"}
        if value is not None:
            env["ACTIONS_ALLOWED_PATTERNS"] = value
        output = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen") as request, \
                contextlib.redirect_stdout(output):
            request.return_value.__enter__.return_value = io.BytesIO(b'{"tree": []}')
            try:
                exec(compile(script, str(workflow), "exec"), {})
            except SystemExit as error:
                code = error.code
            else:
                code = 0
        return code, output.getvalue(), request

    def test_invalid_values_fail_before_any_api_request_with_safe_diagnostic(self):
        for raw, detail in ((None, "missing"), ("", "JSON"), ("{", "JSON"),
                            ('"private-pattern"', "str"), ('{}', "dict"),
                            ('123', "int"), ('null', "NoneType"), ('[123]', "list")):
            with self.subTest(raw=raw):
                code, output, request = self.run_validation(raw)
                self.assertEqual(code, 1)
                request.assert_not_called()
                self.assertIn("JSON array of strings", output)
                self.assertIn(detail, output)
                self.assertNotIn("private-pattern", output)

    def test_arrays_of_strings_and_empty_deny_all_array_are_valid(self):
        for raw in ('[]', '["actions/checkout@*"]'):
            with self.subTest(raw=raw):
                code, _, request = self.run_validation(raw)
                self.assertEqual(code, 0)
                request.assert_called_once()


if __name__ == "__main__":
    unittest.main()

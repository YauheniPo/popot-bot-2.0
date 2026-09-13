import unittest
from unittest import mock

import sonar_review_context


class SonarContextTest(unittest.TestCase):
    def test_main_reports_api_failures_to_the_workflow(self):
        with mock.patch.dict(
            sonar_review_context.os.environ,
            {
                "SONAR_TOKEN": "token", "SONAR_PROJECT_KEY": "project",
                "PR_NUMBER": "31", "BASE_SHA": "base", "HEAD_SHA": "head",
            }, clear=True,
        ), mock.patch.object(sonar_review_context, "build_context", side_effect=RuntimeError("unavailable")):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                sonar_review_context.main()

    @mock.patch.object(sonar_review_context, "_changed_paths", return_value={"src/app.py"})
    @mock.patch.object(sonar_review_context, "_request")
    def test_context_keeps_only_open_issues_in_changed_files(self, request, _changed):
        request.side_effect = [
            {"projectStatus": {"status": "ERROR", "conditions": [{"metricKey": "new_coverage", "status": "ERROR", "actualValue": "99.8", "errorThreshold": "100"}]}},
            {"total": 2, "issues": [
                {"key": "in", "component": "project:src/app.py", "severity": "MAJOR", "type": "CODE_SMELL", "line": 8, "textRange": {"startLine": 8}, "message": "Fix this"},
                {"key": "out", "component": "project:README.md", "severity": "MINOR", "type": "CODE_SMELL", "line": 1, "message": "Ignore this"},
            ]},
        ]

        result = sonar_review_context.build_context("project", "31", "token", "base", "head")

        self.assertEqual(result["quality_gate"], "ERROR")
        self.assertEqual(result["conditions"][0]["actual"], "99.8")
        self.assertEqual([issue["key"] for issue in result["issues"]], ["in"])
        self.assertEqual(result["issues"][0]["path"], "src/app.py")


if __name__ == "__main__":
    unittest.main()

"""Regression tests for standalone Hermes operations scripts."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


OPS_DIR = Path(__file__).parent


class OperationScriptTests(unittest.TestCase):
    def write_executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def read_nul_arguments(self, path: Path) -> list[str]:
        return [
            value.decode("utf-8")
            for value in path.read_bytes().split(b"\0")
            if value
        ]

    def test_api_retry_uses_managed_defaults_and_json_encodes_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            config = temporary / "hermes-ops.conf"
            config.write_text(
                "\n".join(
                    (
                        "HERMES_API_RETRY_ENDPOINT=http://127.0.0.1:9119/api/execute",
                        "HERMES_API_RETRY_MODEL=default/model",
                        "HERMES_API_RETRY_MESSAGE=Hello",
                        "HERMES_API_RETRY_MAX_ATTEMPTS=1",
                        "HERMES_API_RETRY_WAIT_SECONDS=1",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            executable_dir = temporary / "bin"
            executable_dir.mkdir()
            curl_arguments = temporary / "curl-arguments"
            self.write_executable(
                executable_dir / "curl",
                "#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" >\"$CURL_ARGUMENTS\"\nprintf '{\\\"reply\\\":\\\"ok\\\"}\\n200'\n",
            )

            environment = {
                **os.environ,
                "HERMES_OPS_CONFIG": str(config),
                "CURL_ARGUMENTS": str(curl_arguments),
                "PATH": f"{executable_dir}:{os.environ['PATH']}",
            }
            completed = subprocess.run(
                [
                    str(OPS_DIR / "api-retry-loop.sh"),
                    "provider/model",
                    'first line "quoted"\nsecond line',
                ],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            arguments = self.read_nul_arguments(curl_arguments)
            payload = arguments[arguments.index("--data-binary") + 1]
            self.assertEqual(
                json.loads(payload),
                {
                    "model": "provider/model",
                    "messages": [
                        {"role": "user", "content": 'first line "quoted"\nsecond line'}
                    ],
                },
            )

    def test_notify_doctor_url_encodes_plain_text_and_reports_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            executable_dir = temporary / "bin"
            executable_dir.mkdir()
            curl_arguments = temporary / "curl-arguments"
            self.write_executable(
                executable_dir / "hermes",
                "#!/usr/bin/env bash\nprintf 'failed: [needs] *attention* & retry\\n'\nexit 42\n",
            )
            self.write_executable(
                executable_dir / "curl",
                "#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" >\"$CURL_ARGUMENTS\"\n",
            )

            environment = {
                **os.environ,
                "CURL_ARGUMENTS": str(curl_arguments),
                "PATH": f"{executable_dir}:{os.environ['PATH']}",
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_CHAT_ID": "chat id",
            }
            completed = subprocess.run(
                [str(OPS_DIR / "notify-doctor.sh")],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            arguments = self.read_nul_arguments(curl_arguments)
            self.assertNotIn("parse_mode", arguments)
            self.assertIn("--data-urlencode", arguments)
            self.assertIn("chat_id=chat id", arguments)
            self.assertIn(
                "text=❌ Hermes Doctor failed\nfailed: [needs] *attention* & retry",
                arguments,
            )


if __name__ == "__main__":
    unittest.main()

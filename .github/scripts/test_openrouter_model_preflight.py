from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name("openrouter_model_preflight.py")
SPEC = importlib.util.spec_from_file_location("openrouter_model_preflight", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


REQUIRED = frozenset({"max_tokens", "response_format", "structured_outputs", "tools"})


def payload(*endpoints: object) -> object:
    return {"data": {"endpoints": list(endpoints)}}


def endpoint(*capabilities: str, status: int = 0) -> object:
    return {
        "status": status,
        "supported_parameters": list(capabilities),
    }


class ModelSelectionTest(unittest.TestCase):
    def test_retries_transient_metadata_failure(self) -> None:
        expected = payload(endpoint(*REQUIRED))
        http_error = urllib.error.HTTPError(
            "https://openrouter.ai",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b"{}"),
        )
        self.addCleanup(http_error.close)
        response = mock.MagicMock()
        response.__enter__.return_value = io.StringIO(json.dumps(expected))
        with (
            mock.patch.object(
                preflight.urllib.request,
                "urlopen",
                side_effect=[http_error, response],
            ) as urlopen,
            mock.patch.object(preflight.time, "sleep") as sleep,
        ):
            result = preflight._fetch_model("provider/model")

        self.assertEqual(result, expected)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_live_schema_probe_sends_and_validates_a_real_completion(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = io.StringIO(
            json.dumps(
                {
                    "choices": [
                        {"message": {"content": json.dumps({"status": "ok"})}}
                    ]
                }
            )
        )
        with mock.patch.object(
            preflight.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            ready, reason = preflight._probe_json_schema("provider/model", "secret")

        self.assertTrue(ready)
        self.assertIn("passed", reason)
        request = urlopen.call_args.args[0]
        request_body = json.loads(request.data)
        self.assertEqual(request_body["model"], "provider/model")
        self.assertEqual(request_body["response_format"]["type"], "json_schema")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")

    def test_live_schema_probe_rejects_schema_mismatch(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = io.StringIO(
            json.dumps(
                {
                    "choices": [
                        {"message": {"content": json.dumps({"status": "wrong"})}}
                    ]
                }
            )
        )
        with mock.patch.object(
            preflight.urllib.request,
            "urlopen",
            return_value=response,
        ):
            ready, reason = preflight._probe_json_schema("provider/model", "secret")

        self.assertFalse(ready)
        self.assertIn("did not satisfy", reason)

    def test_live_schema_probe_treats_rate_limit_as_inconclusive(self) -> None:
        http_error = urllib.error.HTTPError(
            "https://openrouter.ai",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b"{}"),
        )
        self.addCleanup(http_error.close)
        with mock.patch.object(
            preflight.urllib.request,
            "urlopen",
            side_effect=http_error,
        ):
            ready, reason = preflight._probe_json_schema("provider/model", "secret")

        self.assertIsNone(ready)
        self.assertIn("inconclusive", reason)
        self.assertIn("HTTP 429", reason)

    def test_selects_compatible_primary_and_keeps_compatible_fallback(self) -> None:
        responses = {
            "provider/primary": payload(endpoint(*REQUIRED)),
            "provider/fallback": payload(endpoint(*REQUIRED)),
        }

        selection = preflight.select_models(
            "provider/primary",
            "provider/fallback",
            REQUIRED,
            responses.__getitem__,
        )

        self.assertTrue(selection.primary.ready)
        self.assertTrue(selection.fallback.ready)
        self.assertEqual(selection.selected_model, "provider/primary")
        self.assertEqual(selection.secondary_model, "provider/fallback")

    def test_selects_fallback_when_primary_has_no_active_endpoint(self) -> None:
        responses = {
            "provider/primary": payload(),
            "provider/fallback": payload(endpoint(*REQUIRED)),
        }

        selection = preflight.select_models(
            "provider/primary",
            "provider/fallback",
            REQUIRED,
            responses.__getitem__,
        )

        self.assertFalse(selection.primary.ready)
        self.assertTrue(selection.fallback.ready)
        self.assertEqual(selection.selected_model, "provider/fallback")
        self.assertEqual(selection.secondary_model, "")

    def test_discovers_compatible_free_fallback_when_configured_one_is_down(self) -> None:
        responses = {
            "provider/primary": payload(endpoint(*REQUIRED)),
            "provider/configured:free": payload(),
            "provider/discovered:free": payload(endpoint(*REQUIRED)),
        }
        catalog = {
            "data": [
                {
                    "id": "provider/paid",
                    "supported_parameters": list(REQUIRED),
                },
                {
                    "id": "provider/incomplete:free",
                    "supported_parameters": ["tools"],
                },
                {
                    "id": "provider/discovered:free",
                    "supported_parameters": list(REQUIRED),
                },
            ]
        }

        selection = preflight.select_models(
            "provider/primary",
            "provider/configured:free",
            REQUIRED,
            responses.__getitem__,
            discover_free=True,
            fetch_models=lambda _required: catalog,
        )

        self.assertTrue(selection.primary.ready)
        self.assertEqual(selection.fallback.model, "provider/discovered:free")
        self.assertTrue(selection.fallback.ready)
        self.assertEqual(selection.secondary_model, "provider/discovered:free")

    def test_uses_discovered_free_model_when_primary_and_fallback_are_down(self) -> None:
        responses = {
            "provider/primary": payload(),
            "provider/configured:free": payload(),
            "provider/discovered:free": payload(endpoint(*REQUIRED)),
        }
        catalog = {
            "data": [
                {
                    "id": "provider/discovered:free",
                    "supported_parameters": list(REQUIRED),
                }
            ]
        }

        selection = preflight.select_models(
            "provider/primary",
            "provider/configured:free",
            REQUIRED,
            responses.__getitem__,
            discover_free=True,
            fetch_models=lambda _required: catalog,
        )

        self.assertEqual(selection.selected_model, "provider/discovered:free")
        self.assertEqual(selection.secondary_model, "")

    def test_rejects_endpoint_missing_required_capabilities(self) -> None:
        check = preflight.check_model(
            "provider/model",
            REQUIRED,
            lambda _model: payload(endpoint("max_tokens", "tools")),
        )

        self.assertFalse(check.ready)
        self.assertIn("response_format", check.reason)
        self.assertIn("structured_outputs", check.reason)

    def test_rejects_metadata_compatible_model_when_live_schema_probe_fails(self) -> None:
        check = preflight.check_model(
            "provider/model",
            REQUIRED,
            lambda _model: payload(endpoint(*REQUIRED)),
            lambda _model: (False, "live JSON-schema probe did not satisfy the schema"),
        )

        self.assertFalse(check.ready)
        self.assertIn("did not satisfy", check.reason)

    def test_keeps_metadata_compatible_model_when_live_probe_is_inconclusive(self) -> None:
        check = preflight.check_model(
            "provider/model",
            REQUIRED,
            lambda _model: payload(endpoint(*REQUIRED)),
            lambda _model: (None, "live JSON-schema probe was inconclusive after HTTP 429"),
        )

        self.assertTrue(check.ready)
        self.assertIn("inconclusive", check.reason)

    def test_falls_back_when_primary_fails_the_live_schema_probe(self) -> None:
        responses = {
            "provider/primary": payload(endpoint(*REQUIRED)),
            "provider/fallback": payload(endpoint(*REQUIRED)),
        }

        selection = preflight.select_models(
            "provider/primary",
            "provider/fallback",
            REQUIRED,
            responses.__getitem__,
            probe_model=lambda model: (
                (False, "probe mismatch")
                if model == "provider/primary"
                else (True, "passed the live JSON-schema probe")
            ),
        )

        self.assertFalse(selection.primary.ready)
        self.assertTrue(selection.fallback.ready)
        self.assertEqual(selection.selected_model, "provider/fallback")

    def test_selects_primary_after_transient_probe_when_fallback_is_down(self) -> None:
        responses = {
            "provider/primary": payload(endpoint(*REQUIRED)),
            "provider/fallback": payload(),
        }

        selection = preflight.select_models(
            "provider/primary",
            "provider/fallback",
            REQUIRED,
            responses.__getitem__,
            probe_model=lambda _model: (
                None,
                "live JSON-schema probe was inconclusive after HTTP 429",
            ),
        )

        self.assertTrue(selection.primary.ready)
        self.assertFalse(selection.fallback.ready)
        self.assertEqual(selection.selected_model, "provider/primary")
        self.assertEqual(selection.secondary_model, "")

    def test_ignores_inactive_compatible_endpoint(self) -> None:
        check = preflight.check_model(
            "provider/model",
            REQUIRED,
            lambda _model: payload(endpoint(*REQUIRED, status=-2)),
        )

        self.assertFalse(check.ready)
        self.assertEqual(check.reason, "has no active endpoints")

    def test_rejects_two_unusable_models(self) -> None:
        responses = {
            "provider/primary": payload(),
            "provider/fallback": payload(endpoint("tools")),
        }

        with self.assertRaisesRegex(RuntimeError, "no usable OpenRouter review model"):
            preflight.select_models(
                "provider/primary",
                "provider/fallback",
                REQUIRED,
                responses.__getitem__,
            )

    def test_does_not_retry_the_same_model_as_its_own_fallback(self) -> None:
        calls: list[str] = []

        def fetch(model: str) -> object:
            calls.append(model)
            return payload(endpoint(*REQUIRED))

        selection = preflight.select_models(
            "provider/model",
            "provider/model",
            REQUIRED,
            fetch,
        )

        self.assertEqual(calls, ["provider/model"])
        self.assertTrue(selection.primary.ready)
        self.assertFalse(selection.fallback.ready)
        self.assertEqual(selection.secondary_model, "")

    def test_writes_only_single_line_safe_outputs(self) -> None:
        selection = preflight.ModelSelection(
            preflight.ModelCheck("provider/primary", True, 1, "ready"),
            preflight.ModelCheck("provider/fallback", True, 1, "ready"),
            "provider/primary",
            "provider/fallback",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "github-output"
            preflight._write_github_outputs(selection, str(output_path))
            values = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
            )

        self.assertEqual(values["primary_ready"], "true")
        self.assertEqual(values["fallback_ready"], "true")
        self.assertEqual(values["selected_model"], "provider/primary")


if __name__ == "__main__":
    unittest.main()

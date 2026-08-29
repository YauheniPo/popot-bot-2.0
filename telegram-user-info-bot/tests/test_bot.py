from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import bot  # noqa: E402
from bot import (  # noqa: E402
    BotAPIError,
    collect_user_report,
    find_timezone_details,
    handle_update,
    process_update,
    reverse_geocode_location,
    run_bot,
)


SUCCESSFUL_LOCATION_DETAILS = {
    "reverse_geocoding": {
        "ok": True,
        "data": {
            "city": "Warsaw",
            "country": "Poland",
            "display_name": "Warsaw, Poland",
            "attribution": "© OpenStreetMap contributors",
        },
    },
    "timezone": {
        "ok": True,
        "data": {
            "iana_name": "Europe/Warsaw",
            "utc_offset": "+02:00",
            "local_time": "2026-08-17T12:00:00+02:00",
        },
    },
}


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class FakeAPI:
    def __init__(self, failing_method: str | None = None) -> None:
        self.failing_method = failing_method
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> Any:
        del timeout
        request_payload = payload or {}
        self.calls.append((method, request_payload))
        if method == self.failing_method:
            raise BotAPIError(f"{method}: unavailable")
        if method == "getChat":
            return {"id": request_payload["chat_id"], "type": "private"}
        if method == "getChatMember":
            return {"status": "member", "user": {"id": request_payload["user_id"]}}
        if method == "getUserProfilePhotos":
            offset = request_payload["offset"]
            page_size = 100 if offset == 0 else 1
            return {
                "total_count": 101,
                "photos": [[{"file_id": f"photo-{offset + index}"}]
                           for index in range(page_size)],
            }
        if method == "getUserProfileAudios":
            return {"total_count": 0, "audios": []}
        if method == "getUserPersonalChatMessages":
            return [{"message_id": 7}]
        raise AssertionError(f"Unexpected method: {method}")


class FakeHandlerAPI(FakeAPI):
    def __init__(
        self,
        failing_method: str | None = None,
        fail_document_upload: bool = False,
    ) -> None:
        super().__init__(failing_method=failing_method)
        self.fail_document_upload = fail_document_upload
        self.messages: list[tuple[int | str, str, dict[str, Any] | None]] = []
        self.documents: list[tuple[int | str, str, bytes, str]] = []

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.messages.append((chat_id, text, reply_markup))

    def send_text_document(
        self,
        chat_id: int | str,
        filename: str,
        document: bytes,
        caption: str,
    ) -> None:
        if self.fail_document_upload:
            raise BotAPIError("sendDocument: unavailable")
        self.documents.append((chat_id, filename, document, caption))


class CollectUserReportTest(unittest.TestCase):
    def test_collects_every_profile_photo_page(self) -> None:
        api = FakeAPI()

        report = collect_user_report(
            api,  # type: ignore[arg-type]
            {"id": 42, "is_bot": False, "first_name": "Alice"},
            {"id": 42, "type": "private", "first_name": "Alice"},
        )

        photos = report["profile_photos"]
        self.assertTrue(photos["ok"])
        self.assertEqual(101, len(photos["data"]["photos"]))
        self.assertIsNone(report["user"]["known_fields"]["username"])
        self.assertTrue(
            report["user"]["field_availability"]["first_name"]["returned"]
        )
        self.assertFalse(report["user"]["field_availability"]["username"]["returned"])
        self.assertFalse(
            report["chat_full_info"]["field_availability"]["birthdate"]["returned"]
        )
        self.assertIn("phone_number", report["data_access_notes"])
        photo_calls = [call for call in api.calls if call[0] == "getUserProfilePhotos"]
        self.assertEqual([0, 100], [call[1]["offset"] for call in photo_calls])

    def test_records_optional_api_failure(self) -> None:
        api = FakeAPI(failing_method="getUserProfileAudios")

        report = collect_user_report(
            api,  # type: ignore[arg-type]
            {"id": 42, "is_bot": False, "first_name": "Alice"},
            {"id": 42, "type": "private"},
        )

        self.assertFalse(report["profile_audios"]["ok"])
        self.assertIn("unavailable", report["profile_audios"]["error"])
        self.assertTrue(report["chat_full_info"]["ok"])


class RunBotTest(unittest.TestCase):
    def test_deletes_webhook_after_polling_conflict(self) -> None:
        class ConflictAPI(FakeAPI):
            def __init__(self) -> None:
                super().__init__()
                self.poll_attempts = 0

            def call(
                self,
                method: str,
                payload: dict[str, Any] | None = None,
                *,
                timeout: int | None = None,
            ) -> Any:
                if method == "getMe":
                    self.calls.append((method, payload or {}))
                    return {"username": "test_bot"}
                if method == "getUpdates":
                    self.poll_attempts += 1
                    self.calls.append((method, payload or {}))
                    if self.poll_attempts == 1:
                        raise BotAPIError(
                            "getUpdates: Conflict: can't use getUpdates method "
                            "while webhook is active"
                        )
                    raise KeyboardInterrupt
                if method == "deleteWebhook":
                    self.calls.append((method, payload or {}))
                    return True
                raise AssertionError(f"Unexpected method: {method}")

        api = ConflictAPI()

        with patch("bot.time.sleep"):
            with self.assertRaises(KeyboardInterrupt):
                run_bot(api, poll_timeout=1)

        self.assertEqual(
            ["getMe", "getUpdates", "deleteWebhook", "getUpdates"],
            [method for method, _ in api.calls],
        )
        self.assertEqual(
            {"drop_pending_updates": False},
            api.calls[2][1],
        )


class HandleUpdateTest(unittest.TestCase):
    def test_private_start_returns_text_document(self) -> None:
        api = FakeHandlerAPI()

        handle_update(
            api,  # type: ignore[arg-type]
            {
                "update_id": 1,
                "message": {
                    "text": "/start",
                    "from": {"id": 42, "is_bot": False, "first_name": "Alice"},
                    "chat": {"id": 42, "type": "private", "first_name": "Alice"},
                },
            },
        )

        self.assertEqual(1, len(api.messages))
        self.assertEqual(1, len(api.documents))
        _, filename, document, _ = api.documents[0]
        self.assertEqual("telegram-user-42.txt", filename)
        self.assertTrue(document.startswith(b"\xef\xbb\xbf"))
        text = document.decode("utf-8-sig")
        self.assertIn("USER DATA AVAILABLE TO THE TELEGRAM BOT", text)
        self.assertIn("id: 42", text)
        self.assertIn("update_id: 1", text)
        self.assertNotIn("**", text)
        self.assertNotIn("\\_", text)

    def test_group_command_does_not_collect_or_publish_user_data(self) -> None:
        api = FakeHandlerAPI()

        handle_update(
            api,  # type: ignore[arg-type]
            {
                "update_id": 2,
                "message": {
                    "text": "/me",
                    "from": {"id": 42, "is_bot": False, "first_name": "Alice"},
                    "chat": {"id": -100, "type": "supergroup", "title": "Test"},
                },
            },
        )

        self.assertEqual([], api.calls)
        self.assertEqual([], api.documents)
        self.assertEqual(-100, api.messages[0][0])

    def test_share_command_displays_consent_buttons(self) -> None:
        api = FakeHandlerAPI()

        handle_update(
            api,  # type: ignore[arg-type]
            {
                "update_id": 3,
                "message": {
                    "text": "/share",
                    "from": {"id": 42, "is_bot": False, "first_name": "Alice"},
                    "chat": {"id": 42, "type": "private"},
                },
            },
        )

        reply_markup = api.messages[0][2]
        self.assertIsNotNone(reply_markup)
        buttons = reply_markup["keyboard"][0]  # type: ignore[index]
        self.assertEqual("Share phone number", buttons[0]["text"])
        self.assertEqual("Share location", buttons[1]["text"])
        self.assertTrue(buttons[0]["request_contact"])
        self.assertTrue(buttons[1]["request_location"])
        consent_text = api.messages[0][1]
        self.assertIn("exact latitude and longitude", consent_text)
        self.assertIn("OpenStreetMap Nominatim", consent_text)
        self.assertIn("public", consent_text)
        self.assertEqual([], api.documents)

    def test_own_contact_is_added_to_report(self) -> None:
        api = FakeHandlerAPI()

        handle_update(
            api,  # type: ignore[arg-type]
            {
                "update_id": 4,
                "message": {
                    "contact": {
                        "phone_number": "+48123456789",
                        "first_name": "Alice",
                        "user_id": 42,
                    },
                    "from": {"id": 42, "is_bot": False, "first_name": "Alice"},
                    "chat": {"id": 42, "type": "private"},
                },
            },
        )

        self.assertEqual(1, len(api.documents))
        text = api.documents[0][2].decode("utf-8-sig")
        self.assertIn("DATA EXPLICITLY SHARED BY THE USER", text)
        self.assertIn("type: contact", text)
        self.assertIn("verified_as_sender: true", text)
        self.assertIn("+48123456789", text)
        self.assertNotIn("\\u", text)

    def test_someone_elses_contact_is_rejected(self) -> None:
        api = FakeHandlerAPI()

        handle_update(
            api,  # type: ignore[arg-type]
            {
                "update_id": 5,
                "message": {
                    "contact": {
                        "phone_number": "+48111111111",
                        "first_name": "Bob",
                        "user_id": 99,
                    },
                    "from": {"id": 42, "is_bot": False, "first_name": "Alice"},
                    "chat": {"id": 42, "type": "private"},
                },
            },
        )

        self.assertEqual([], api.calls)
        self.assertEqual([], api.documents)
        self.assertIn("Contact rejected", api.messages[0][1])

    def test_location_is_added_to_report(self) -> None:
        api = FakeHandlerAPI()

        with patch("bot.enrich_location", return_value=SUCCESSFUL_LOCATION_DETAILS):
            handle_update(
                api,  # type: ignore[arg-type]
                {
                    "update_id": 6,
                    "message": {
                        "location": {"latitude": 52.2297, "longitude": 21.0122},
                        "from": {
                            "id": 42,
                            "is_bot": False,
                            "first_name": "Alice",
                        },
                        "chat": {"id": 42, "type": "private"},
                    },
                },
            )

        text = api.documents[0][2].decode("utf-8-sig")
        self.assertIn("type: location", text)
        self.assertIn("latitude: 52.2297", text)
        self.assertIn("city: Warsaw", text)
        self.assertIn("iana_name: Europe/Warsaw", text)
        self.assertIn("City or locality: Warsaw", api.messages[1][1])
        self.assertIn("Time zone: Europe/Warsaw", api.messages[1][1])

    def test_location_lookup_error_is_sent_as_chat_warning(self) -> None:
        api = FakeHandlerAPI()
        failed_details = {
            "reverse_geocoding": {
                "ok": False,
                "error": "The place service is unavailable.",
            },
            "timezone": {"ok": False, "error": "No time zone was found."},
        }

        with patch("bot.enrich_location", return_value=failed_details):
            handle_update(
                api,  # type: ignore[arg-type]
                {
                    "update_id": 9,
                    "message": {
                        "location": {"latitude": 52.2297, "longitude": 21.0122},
                        "from": {
                            "id": 42,
                            "is_bot": False,
                            "first_name": "Alice",
                        },
                        "chat": {"id": 42, "type": "private"},
                    },
                },
            )

        self.assertEqual(1, len(api.documents))
        self.assertIn("The report is incomplete", api.messages[-1][1])
        self.assertIn("Place lookup", api.messages[-1][1])
        self.assertIn("Time zone lookup", api.messages[-1][1])

    def test_optional_api_error_is_sent_as_chat_warning(self) -> None:
        api = FakeHandlerAPI(failing_method="getUserProfileAudios")

        handle_update(
            api,  # type: ignore[arg-type]
            {
                "update_id": 7,
                "message": {
                    "text": "/me",
                    "from": {"id": 42, "is_bot": False, "first_name": "Alice"},
                    "chat": {"id": 42, "type": "private"},
                },
            },
        )

        self.assertEqual(1, len(api.documents))
        self.assertIn("The report is incomplete", api.messages[-1][1])
        self.assertIn("getUserProfileAudios", api.messages[-1][1])

    def test_fatal_request_error_is_logged_and_sent_to_chat(self) -> None:
        api = FakeHandlerAPI(fail_document_upload=True)
        update = {
            "update_id": 8,
            "message": {
                "text": "/me",
                "from": {"id": 42, "is_bot": False, "first_name": "Alice"},
                "chat": {"id": 42, "type": "private"},
            },
        }

        with self.assertLogs("bot", level="ERROR") as logs:
            process_update(api, update)  # type: ignore[arg-type]

        self.assertIn("Telegram API error", "\n".join(logs.output))
        self.assertIn("Could not process request 8", api.messages[-1][1])


class LocationLookupTest(unittest.TestCase):
    def test_reverse_geocoding_returns_extracted_and_raw_data(self) -> None:
        response = FakeHTTPResponse(
            {
                "place_id": 123,
                "display_name": "Warsaw, Masovian Voivodeship, Poland",
                "address": {
                    "city": "Warsaw",
                    "state": "Masovian Voivodeship",
                    "country": "Poland",
                    "country_code": "pl",
                },
            }
        )

        with (
            patch("bot._wait_for_nominatim_slot"),
            patch("bot.urlopen", return_value=response) as mocked_urlopen,
            patch.dict(
                "os.environ",
                {
                    "NOMINATIM_USER_AGENT": "telegram-user-info-bot-tests/1.0",
                    "NOMINATIM_LANGUAGE": "en",
                },
            ),
        ):
            result = reverse_geocode_location(52.2297, 21.0122)

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual("telegram-user-info-bot-tests/1.0", request.get_header("User-agent"))
        self.assertEqual("en", request.get_header("Accept-language"))
        self.assertIn("format=jsonv2", request.full_url)
        self.assertEqual("Warsaw", result["city"])
        self.assertEqual("Poland", result["country"])
        self.assertEqual(123, result["raw_response"]["place_id"])

    def test_timezone_is_calculated_locally(self) -> None:
        class FakeTimezoneFinder:
            def timezone_at(self, *, lng: float, lat: float) -> str:
                self.coordinates = (lat, lng)
                return "Etc/UTC"

        finder = FakeTimezoneFinder()
        with patch("bot._get_timezone_finder", return_value=finder):
            result = find_timezone_details(52.2297, 21.0122)

        self.assertEqual((52.2297, 21.0122), finder.coordinates)
        self.assertEqual("Etc/UTC", result["iana_name"])
        self.assertEqual("+00:00", result["utc_offset"])
        self.assertIn("+00:00", result["local_time"])

    def test_invalid_coordinates_are_rejected_before_network_request(self) -> None:
        with patch("bot.urlopen") as mocked_urlopen:
            with self.assertRaises(bot.LocationLookupError):
                reverse_geocode_location(100.0, 21.0122)

        mocked_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

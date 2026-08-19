#!/usr/bin/env python3
"""Telegram bot that returns the caller data exposed by the Bot API."""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from timezonefinder import TimezoneFinder
except ImportError:  # The bot reports a clear per-request error if setup was skipped.
    TimezoneFinder = None  # type: ignore[assignment,misc]


API_BASE_URL = "https://api.telegram.org"
DEFAULT_POLL_TIMEOUT = 30
MAX_POLL_TIMEOUT = 50
PROFILE_PAGE_SIZE = 100
REPORT_SCHEMA_VERSION = 4
BOT_API_REFERENCE = "https://core.telegram.org/bots/api"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_ATTRIBUTION = "© OpenStreetMap contributors"
NOMINATIM_ATTRIBUTION_URL = "https://www.openstreetmap.org/copyright"
NOMINATIM_MIN_INTERVAL_SECONDS = 1.0
NOMINATIM_REQUEST_TIMEOUT = 10
DEFAULT_NOMINATIM_LANGUAGE = "en"
DEFAULT_NOMINATIM_USER_AGENT = "telegram-user-info-bot/1.0"

_last_nominatim_request_at = 0.0
_timezone_finder: Any = None

KNOWN_USER_FIELDS = (
    "id",
    "is_bot",
    "first_name",
    "last_name",
    "username",
    "language_code",
    "is_premium",
    "added_to_attachment_menu",
    "can_join_groups",
    "can_read_all_group_messages",
    "supports_guest_queries",
    "supports_inline_queries",
    "can_connect_to_business",
    "has_main_web_app",
    "has_topics_enabled",
    "allows_users_to_create_topics",
    "can_manage_bots",
    "supports_join_request_queries",
)

KNOWN_CHAT_FULL_INFO_FIELDS = (
    "id",
    "type",
    "title",
    "username",
    "first_name",
    "last_name",
    "is_forum",
    "is_direct_messages",
    "accent_color_id",
    "max_reaction_count",
    "photo",
    "active_usernames",
    "birthdate",
    "business_intro",
    "business_location",
    "business_opening_hours",
    "personal_chat",
    "parent_chat",
    "available_reactions",
    "background_custom_emoji_id",
    "profile_accent_color_id",
    "profile_background_custom_emoji_id",
    "emoji_status_custom_emoji_id",
    "emoji_status_expiration_date",
    "bio",
    "has_private_forwards",
    "has_restricted_voice_and_video_messages",
    "join_to_send_messages",
    "join_by_request",
    "description",
    "invite_link",
    "pinned_message",
    "permissions",
    "accepted_gift_types",
    "can_send_gift",
    "can_send_paid_media",
    "slow_mode_delay",
    "unrestrict_boost_count",
    "message_auto_delete_time",
    "has_aggressive_anti_spam_enabled",
    "has_hidden_members",
    "has_protected_content",
    "has_visible_history",
    "sticker_set_name",
    "can_set_sticker_set",
    "custom_emoji_sticker_set_name",
    "linked_chat_id",
    "location",
    "rating",
    "first_profile_audio",
    "unique_gift_colors",
    "paid_message_star_count",
    "guard_bot",
    "community",
)

DATA_ACCESS_NOTES = {
    "phone_number": (
        "This is not a Bot API profile field. The bot receives a phone number "
        "only when the user explicitly sends a Contact or provides it through "
        "a supported Telegram flow."
    ),
    "email": (
        "This is not a Bot API profile field. A user may explicitly provide it, "
        "for example during a payment flow."
    ),
    "ip_address_and_device": (
        "The Bot API does not provide an IP address or device information."
    ),
    "private_messages_and_contacts": (
        "The Bot API does not provide private message history or a contact list."
    ),
    "location": (
        "The current location is available only after the user explicitly sends "
        "it; business_location may be returned by getChat when applicable."
    ),
    "location_enrichment": (
        "Explicitly shared coordinates are sent to OpenStreetMap Nominatim to "
        "identify the place. The time zone is determined locally."
    ),
    "optional_fields": (
        "A missing field means it is not applicable, hidden by privacy settings, "
        "or was not returned by Telegram."
    ),
}

REPORT_COMMANDS = {"/start", "/me"}
SHARE_COMMAND = "/share"
CLOSE_KEYBOARD_TEXT = "Hide buttons"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger(__name__)


class BotAPIError(RuntimeError):
    """A safe-to-log Telegram Bot API error that never contains the bot token."""


class LocationLookupError(RuntimeError):
    """A location lookup error safe to include in logs and the user report."""


class TelegramBotAPI:
    def __init__(self, token: str, request_timeout: int = 30) -> None:
        self._base_url = f"{API_BASE_URL}/bot{token}/"
        self._request_timeout = request_timeout

    def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> Any:
        body = json.dumps(payload or {}).encode("utf-8")
        return self._post(
            method,
            body,
            "application/json; charset=utf-8",
            timeout=timeout,
        )

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def send_text_document(
        self,
        chat_id: int | str,
        filename: str,
        document: bytes,
        caption: str,
    ) -> Any:
        boundary = f"----telegram-user-data-{secrets.token_hex(12)}"
        body = _build_multipart_body(
            boundary,
            fields={"chat_id": str(chat_id), "caption": caption},
            file_field="document",
            filename=filename,
            content_type="text/plain; charset=utf-8",
            file_content=document,
        )
        return self._post(
            "sendDocument",
            body,
            f"multipart/form-data; boundary={boundary}",
        )

    def _post(
        self,
        method: str,
        body: bytes,
        content_type: str,
        *,
        timeout: int | None = None,
    ) -> Any:
        log_request = method != "getUpdates"
        if log_request:
            LOGGER.info("Telegram API request started: %s", method)
        request = Request(
            f"{self._base_url}{method}",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )

        try:
            with urlopen(
                request,
                timeout=timeout if timeout is not None else self._request_timeout,
            ) as response:
                raw_response = response.read()
        except HTTPError as error:
            description = _telegram_error_description(error.read(), f"HTTP {error.code}")
            raise BotAPIError(f"{method}: {description}") from None
        except (URLError, TimeoutError) as error:
            reason = getattr(error, "reason", "network timeout")
            raise BotAPIError(f"{method}: network error ({reason})") from None

        try:
            parsed = json.loads(raw_response)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BotAPIError(f"{method}: Telegram returned invalid JSON") from None

        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            description = (
                parsed.get("description", "unknown Telegram API error")
                if isinstance(parsed, dict)
                else "unexpected Telegram API response"
            )
            raise BotAPIError(f"{method}: {description}")

        if log_request:
            LOGGER.info("Telegram API request completed: %s", method)
        return parsed.get("result")


def _telegram_error_description(raw_response: bytes, fallback: str) -> str:
    try:
        parsed = json.loads(raw_response)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return fallback
    if isinstance(parsed, dict):
        return str(parsed.get("description", fallback))
    return fallback


def _build_multipart_body(
    boundary: str,
    *,
    fields: dict[str, str],
    file_field: str,
    filename: str,
    content_type: str,
    file_content: bytes,
) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            )
        )

    chunks.extend(
        (
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"\r\n'
            ).encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            file_content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    return b"".join(chunks)


def _capture(section: str, operation: Callable[[], Any]) -> dict[str, Any]:
    LOGGER.info("Collecting report section: %s", section)
    try:
        data = operation()
    except BotAPIError as error:
        LOGGER.warning("Report section %s is unavailable: %s", section, error)
        return {"ok": False, "error": str(error)}
    LOGGER.info("Report section collected: %s", section)
    return {"ok": True, "data": data}


def _field_availability(
    data: dict[str, Any],
    known_fields: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    return {
        field: {"returned": field in data, "value": data.get(field)}
        for field in known_fields
    }


def _text_scalar(value: Any) -> str:
    if value is None:
        return "not returned"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text:
        return "empty string"
    return (
        text.replace("\r\n", " ⏎ ")
        .replace("\r", " ⏎ ")
        .replace("\n", " ⏎ ")
        .replace("\t", "  ")
    )


def _render_text_tree(value: Any, level: int = 0) -> list[str]:
    indent = "  " * level
    if isinstance(value, dict):
        if not value:
            return [f"{indent}no data"]
        lines: list[str] = []
        for key, item in value.items():
            label = str(key)
            if isinstance(item, (dict, list)):
                lines.append(f"{indent}{label}:")
                lines.extend(_render_text_tree(item, level + 1))
            else:
                lines.append(f"{indent}{label}: {_text_scalar(item)}")
        return lines

    if isinstance(value, list):
        if not value:
            return [f"{indent}empty list"]
        lines = []
        for index, item in enumerate(value, start=1):
            if isinstance(item, (dict, list)):
                lines.append(f"{indent}Item {index}:")
                lines.extend(_render_text_tree(item, level + 1))
            else:
                lines.append(f"{indent}{_text_scalar(item)}")
        return lines

    return [f"{indent}{_text_scalar(value)}"]


def render_report_text(report: dict[str, Any]) -> str:
    section_titles = {
        "report_schema_version": "Report schema version",
        "generated_at": "Generated at",
        "telegram_bot_api_reference": "Telegram Bot API documentation",
        "explanation": "How to read this report",
        "data_access_notes": "Data access limitations",
        "incoming_update_raw": "Complete incoming Telegram Update",
        "explicitly_shared_data": "Data explicitly shared by the user",
        "user": "User",
        "source_chat": "Source chat",
        "chat_full_info": "Complete chat information",
        "chat_member": "Chat member information",
        "profile_photos": "Profile photos",
        "profile_audios": "Profile audio files",
        "personal_chat_messages": "Personal channel messages",
    }
    lines = [
        "USER DATA AVAILABLE TO THE TELEGRAM BOT",
        "",
        (
            "This text report contains every raw field received by the bot and "
            "availability markers for fields that were not returned."
        ),
    ]
    for key, value in report.items():
        title = section_titles.get(key, key)
        lines.extend(("", title.upper(), ""))
        lines.extend(_render_text_tree(value))
    lines.append("")
    return "\n".join(lines)


def _collect_profile_collection(
    api: TelegramBotAPI,
    *,
    method: str,
    collection_key: str,
    user_id: int,
) -> dict[str, Any]:
    collected: list[Any] = []
    offset = 0
    total_count = 0

    while True:
        result = api.call(
            method,
            {"user_id": user_id, "offset": offset, "limit": PROFILE_PAGE_SIZE},
        )
        if not isinstance(result, dict):
            raise BotAPIError(f"{method}: unexpected result type")

        page = result.get(collection_key, [])
        if not isinstance(page, list):
            raise BotAPIError(f"{method}: unexpected {collection_key} type")

        reported_total = result.get("total_count", len(collected) + len(page))
        if isinstance(reported_total, int) and reported_total >= 0:
            total_count = reported_total

        collected.extend(page)
        offset += len(page)
        if not page or offset >= total_count:
            break

    return {"total_count": total_count, collection_key: collected}


def collect_user_report(
    api: TelegramBotAPI,
    user: dict[str, Any],
    source_chat: dict[str, Any],
    incoming_update: dict[str, Any] | None = None,
    explicitly_shared_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user_id = user.get("id")
    chat_id = source_chat.get("id")
    if not isinstance(user_id, int) or not isinstance(chat_id, (int, str)):
        raise BotAPIError("Incoming Telegram message has no valid user or chat id")

    chat_full_info = _capture(
        "chat_full_info",
        lambda: api.call("getChat", {"chat_id": chat_id}),
    )
    chat_full_info_data = chat_full_info.get("data")
    if not isinstance(chat_full_info_data, dict):
        chat_full_info_data = {}
    chat_full_info["field_availability"] = _field_availability(
        chat_full_info_data,
        KNOWN_CHAT_FULL_INFO_FIELDS,
    )

    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "telegram_bot_api_reference": BOT_API_REFERENCE,
        "explanation": (
            "received_fields and incoming_update_raw contain exact Telegram data; "
            "field_availability shows known fields with returned=true/false; "
            "failed API calls are recorded with ok=false"
        ),
        "data_access_notes": DATA_ACCESS_NOTES,
        "incoming_update_raw": incoming_update,
        "explicitly_shared_data": explicitly_shared_data,
        "user": {
            "received_fields": user,
            "known_fields": {field: user.get(field) for field in KNOWN_USER_FIELDS},
            "field_availability": _field_availability(user, KNOWN_USER_FIELDS),
        },
        "source_chat": source_chat,
        "chat_full_info": chat_full_info,
        "chat_member": _capture(
            "chat_member",
            lambda: api.call(
                "getChatMember",
                {"chat_id": chat_id, "user_id": user_id},
            )
        ),
        "profile_photos": _capture(
            "profile_photos",
            lambda: _collect_profile_collection(
                api,
                method="getUserProfilePhotos",
                collection_key="photos",
                user_id=user_id,
            )
        ),
        "profile_audios": _capture(
            "profile_audios",
            lambda: _collect_profile_collection(
                api,
                method="getUserProfileAudios",
                collection_key="audios",
                user_id=user_id,
            )
        ),
        "personal_chat_messages": _capture(
            "personal_chat_messages",
            lambda: api.call(
                "getUserPersonalChatMessages",
                {"user_id": user_id, "limit": 20},
            )
        ),
    }


def _message_command(message: dict[str, Any]) -> str | None:
    text = message.get("text")
    if not isinstance(text, str) or not text.startswith("/"):
        return None
    return text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()


def _consent_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [
            [
                {"text": "Share phone number", "request_contact": True},
                {"text": "Share location", "request_location": True},
            ],
            [{"text": CLOSE_KEYBOARD_TEXT}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "Choose data to share voluntarily",
    }


def _coordinates_are_valid(latitude: Any, longitude: Any) -> bool:
    values_are_numbers = all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        for value in (latitude, longitude)
    )
    return bool(
        values_are_numbers
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


def _wait_for_nominatim_slot() -> None:
    global _last_nominatim_request_at

    elapsed = time.monotonic() - _last_nominatim_request_at
    remaining = NOMINATIM_MIN_INTERVAL_SECONDS - elapsed
    if remaining > 0:
        time.sleep(remaining)
    _last_nominatim_request_at = time.monotonic()


def reverse_geocode_location(latitude: float, longitude: float) -> dict[str, Any]:
    if not _coordinates_are_valid(latitude, longitude):
        raise LocationLookupError("Cannot identify the place: invalid coordinates.")

    query = urlencode(
        {
            "format": "jsonv2",
            "lat": latitude,
            "lon": longitude,
            "addressdetails": 1,
            "zoom": 18,
        }
    )
    user_agent = (
        os.environ.get("NOMINATIM_USER_AGENT", "").strip()
        or DEFAULT_NOMINATIM_USER_AGENT
    )
    language = (
        os.environ.get("NOMINATIM_LANGUAGE", "").strip()
        or DEFAULT_NOMINATIM_LANGUAGE
    )
    request = Request(
        f"{NOMINATIM_REVERSE_URL}?{query}",
        headers={
            "Accept": "application/json",
            "Accept-Language": language,
            "User-Agent": user_agent,
        },
        method="GET",
    )

    _wait_for_nominatim_slot()
    LOGGER.info("OpenStreetMap reverse geocoding started")
    try:
        with urlopen(request, timeout=NOMINATIM_REQUEST_TIMEOUT) as response:
            raw_response = response.read()
    except HTTPError as error:
        raise LocationLookupError(
            f"OpenStreetMap Nominatim returned HTTP {error.code}."
        ) from None
    except (URLError, TimeoutError):
        raise LocationLookupError(
            "OpenStreetMap Nominatim is unavailable or did not respond in time."
        ) from None

    try:
        result = json.loads(raw_response)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise LocationLookupError(
            "OpenStreetMap Nominatim returned an invalid response."
        ) from None

    if not isinstance(result, dict):
        raise LocationLookupError(
            "OpenStreetMap Nominatim returned an unexpected response format."
        )
    if result.get("error"):
        raise LocationLookupError(
            "OpenStreetMap Nominatim could not identify the place."
        )

    address = result.get("address")
    if not isinstance(address, dict):
        address = {}
    city = next(
        (
            address[field]
            for field in (
                "city",
                "town",
                "village",
                "municipality",
                "hamlet",
                "county",
            )
            if isinstance(address.get(field), str) and address[field]
        ),
        None,
    )
    LOGGER.info("OpenStreetMap reverse geocoding completed")
    return {
        "source": "OpenStreetMap Nominatim",
        "attribution": NOMINATIM_ATTRIBUTION,
        "attribution_url": NOMINATIM_ATTRIBUTION_URL,
        "city": city,
        "country": address.get("country"),
        "country_code": address.get("country_code"),
        "state": address.get("state"),
        "county": address.get("county"),
        "postcode": address.get("postcode"),
        "road": address.get("road"),
        "house_number": address.get("house_number"),
        "display_name": result.get("display_name"),
        "address": address,
        "raw_response": result,
    }


def _get_timezone_finder() -> Any:
    global _timezone_finder

    if TimezoneFinder is None:
        raise LocationLookupError(
            "The timezonefinder package is not installed. "
            "Install the dependencies from requirements.txt."
        )
    if _timezone_finder is None:
        _timezone_finder = TimezoneFinder()
    return _timezone_finder


def _format_utc_offset(offset: timedelta | None) -> str | None:
    if offset is None:
        return None
    total_minutes = int(offset.total_seconds() / 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def find_timezone_details(latitude: float, longitude: float) -> dict[str, Any]:
    if not _coordinates_are_valid(latitude, longitude):
        raise LocationLookupError(
            "Cannot identify the time zone: invalid coordinates."
        )

    LOGGER.info("Local timezone lookup started")
    try:
        zone_name = _get_timezone_finder().timezone_at(
            lng=longitude,
            lat=latitude,
        )
    except LocationLookupError:
        raise
    except Exception:
        LOGGER.exception("Local timezone lookup failed")
        raise LocationLookupError(
            "The time zone could not be determined locally."
        ) from None

    if not isinstance(zone_name, str) or not zone_name:
        raise LocationLookupError("No time zone was found for this location.")

    try:
        local_time = datetime.now(ZoneInfo(zone_name))
    except (ZoneInfoNotFoundError, ValueError):
        raise LocationLookupError(
            f"Time zone {zone_name} was found, but its system data is unavailable."
        ) from None

    daylight_saving = local_time.dst()
    LOGGER.info("Local timezone lookup completed")
    return {
        "iana_name": zone_name,
        "local_time": local_time.isoformat(timespec="seconds"),
        "utc_offset": _format_utc_offset(local_time.utcoffset()),
        "abbreviation": local_time.tzname(),
        "daylight_saving_time": bool(
            daylight_saving is not None and daylight_saving != timedelta(0)
        ),
        "calculated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "calculation": "Calculated locally with timezonefinder and zoneinfo.",
    }


def _capture_location_lookup(
    section: str,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    try:
        return {"ok": True, "data": operation()}
    except LocationLookupError as error:
        LOGGER.warning("Location section %s is unavailable: %s", section, error)
        return {"ok": False, "error": str(error)}


def enrich_location(latitude: float, longitude: float) -> dict[str, Any]:
    return {
        "reverse_geocoding": _capture_location_lookup(
            "reverse_geocoding",
            lambda: reverse_geocode_location(latitude, longitude),
        ),
        "timezone": _capture_location_lookup(
            "timezone",
            lambda: find_timezone_details(latitude, longitude),
        ),
    }


def _location_summary(location_details: dict[str, Any]) -> str:
    lines = ["Location processed."]

    place_section = location_details.get("reverse_geocoding")
    place = place_section.get("data") if isinstance(place_section, dict) else None
    if isinstance(place, dict):
        lines.append(f"City or locality: {_text_scalar(place.get('city'))}")
        lines.append(f"Country: {_text_scalar(place.get('country'))}")
        lines.append(f"Address: {_text_scalar(place.get('display_name'))}")
        lines.append(f"Place data source: {NOMINATIM_ATTRIBUTION}")
    else:
        lines.append("The place could not be identified.")

    timezone_section = location_details.get("timezone")
    timezone_data = (
        timezone_section.get("data") if isinstance(timezone_section, dict) else None
    )
    if isinstance(timezone_data, dict):
        lines.append(
            f"Time zone: {_text_scalar(timezone_data.get('iana_name'))}"
        )
        lines.append(
            f"UTC offset: {_text_scalar(timezone_data.get('utc_offset'))}"
        )
        lines.append(
            f"Local time: {_text_scalar(timezone_data.get('local_time'))}"
        )
    else:
        lines.append("The time zone could not be identified.")

    lines.append("Generating a new text report...")
    return "\n".join(lines)[:3500]


def _explicitly_shared_data(
    message: dict[str, Any],
    user_id: int,
) -> tuple[dict[str, Any] | None, str | None]:
    contact = message.get("contact")
    if isinstance(contact, dict):
        contact_user_id = contact.get("user_id")
        if contact_user_id != user_id:
            return None, (
                "Contact rejected: Telegram did not confirm that the phone number "
                "belongs to the sender. Use the Share phone number button."
            )
        return {
            "type": "contact",
            "consent": "The user explicitly shared their contact through Telegram.",
            "verified_as_sender": True,
            "data": contact,
        }, None

    location = message.get("location")
    if isinstance(location, dict):
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if not _coordinates_are_valid(latitude, longitude):
            return None, "Telegram sent a location without valid coordinates."
        return {
            "type": "location",
            "consent": "The user explicitly shared a location through Telegram.",
            "sender_verified_by_private_chat": True,
            "freshness_note": (
                "The Bot API cannot confirm whether this point is the user's "
                "current location or a place selected by the user."
            ),
            "data": location,
        }, None

    return None, None


def _send_user_report(
    api: TelegramBotAPI,
    update: dict[str, Any],
    user: dict[str, Any],
    chat: dict[str, Any],
    explicitly_shared_data: dict[str, Any] | None = None,
) -> None:
    chat_id = chat["id"]
    LOGGER.info("User report generation started")
    report = collect_user_report(
        api,
        user,
        chat,
        incoming_update=update,
        explicitly_shared_data=explicitly_shared_data,
    )
    # UTF-8 BOM helps Telegram/iOS Quick Look recognize Cyrillic in text files.
    document = render_report_text(report).encode("utf-8-sig")
    LOGGER.info("User report generated: %s bytes", len(document))
    user_id = user.get("id")
    safe_user_id = str(user_id) if isinstance(user_id, int) else "unknown"
    caption = (
        "Report containing explicitly shared data and available Telegram data."
        if explicitly_shared_data is not None
        else (
            "Complete report: raw Update, available User and ChatFullInfo fields, "
            "photos, audio files, and conditionally available Telegram data."
        )
    )
    api.send_text_document(
        chat_id,
        f"telegram-user-{safe_user_id}.txt",
        document,
        caption,
    )
    LOGGER.info("User report document sent")

    report_errors = _report_errors(report)
    if report_errors:
        warning_lines = [
            "The report is incomplete. Some data is unavailable:",
            *(f"- {section}: {error}" for section, error in report_errors),
        ]
        api.send_message(chat_id, "\n".join(warning_lines)[:3500])
        LOGGER.warning("User report sent with %s unavailable section(s)", len(report_errors))


def _report_errors(report: dict[str, Any]) -> list[tuple[str, str]]:
    section_names = {
        "chat_full_info": "Complete chat information",
        "chat_member": "Chat member information",
        "profile_photos": "Profile photos",
        "profile_audios": "Profile audio files",
        "personal_chat_messages": "Personal channel messages",
    }
    errors: list[tuple[str, str]] = []
    for section_name, section_label in section_names.items():
        section = report.get(section_name)
        if isinstance(section, dict) and section.get("ok") is False:
            errors.append(
                (section_label, str(section.get("error", "unknown error")))
            )

    shared_data = report.get("explicitly_shared_data")
    location_details = (
        shared_data.get("location_details")
        if isinstance(shared_data, dict)
        else None
    )
    if isinstance(location_details, dict):
        location_section_names = {
            "reverse_geocoding": "Place lookup",
            "timezone": "Time zone lookup",
        }
        for section_name, section_label in location_section_names.items():
            section = location_details.get(section_name)
            if isinstance(section, dict) and section.get("ok") is False:
                errors.append(
                    (
                        section_label,
                        str(section.get("error", "unknown error")),
                    )
                )
    return errors


def handle_update(api: TelegramBotAPI, update: dict[str, Any]) -> None:
    message = update.get("message")
    if not isinstance(message, dict):
        return

    chat = message.get("chat")
    user = message.get("from")
    if not isinstance(chat, dict) or not isinstance(user, dict):
        return

    chat_id = chat.get("id")
    if not isinstance(chat_id, (int, str)):
        return

    user_id = user.get("id")
    if not isinstance(user_id, int):
        return

    command = _message_command(message)
    is_close_request = message.get("text") == CLOSE_KEYBOARD_TEXT
    has_explicit_data = isinstance(message.get("contact"), dict) or isinstance(
        message.get("location"), dict
    )
    if (
        command not in REPORT_COMMANDS
        and command != SHARE_COMMAND
        and not is_close_request
        and not has_explicit_data
    ):
        return

    event_name = command or (
        "contact"
        if isinstance(message.get("contact"), dict)
        else "location" if isinstance(message.get("location"), dict) else "close_keyboard"
    )
    LOGGER.info("Handling user request: %s", event_name)

    if chat.get("type") != "private":
        LOGGER.warning("Rejected data request outside a private chat")
        api.send_message(
            chat_id,
            "To avoid exposing personal data, use this command in a private "
            "chat with the bot.",
        )
        return

    if is_close_request:
        api.send_message(
            chat_id,
            "The keyboard is hidden. Use /share to open it again.",
            reply_markup={"remove_keyboard": True},
        )
        LOGGER.info("Consent keyboard removed")
        return

    if command == SHARE_COMMAND:
        api.send_message(
            chat_id,
            (
                "Choose which data to share voluntarily. Telegram will display "
                "a system confirmation. If you choose Share location and confirm, "
                "the bot will send your exact latitude and longitude to the public "
                "OpenStreetMap Nominatim service to identify the place. The time "
                "zone is determined locally. The bot will return the data in a "
                "text file and will not save it to disk."
            ),
            reply_markup=_consent_keyboard(),
        )
        LOGGER.info("Consent keyboard sent")
        return

    explicitly_shared_data, validation_error = _explicitly_shared_data(
        message,
        user_id,
    )
    if validation_error is not None:
        LOGGER.warning("Explicitly shared data was rejected during validation")
        api.send_message(chat_id, validation_error)
        return
    if explicitly_shared_data is not None:
        LOGGER.info("Explicitly shared data accepted: %s", explicitly_shared_data["type"])
        if explicitly_shared_data["type"] == "location":
            api.send_message(
                chat_id,
                "Location received. Identifying the place and time zone...",
            )
            location = explicitly_shared_data["data"]
            location_details = enrich_location(
                location["latitude"],
                location["longitude"],
            )
            explicitly_shared_data["location_details"] = location_details
            api.send_message(chat_id, _location_summary(location_details))
        else:
            api.send_message(
                chat_id,
                "Data received. Generating a new text report...",
            )
        _send_user_report(api, update, user, chat, explicitly_shared_data)
        return

    api.send_message(
        chat_id,
        "Generating a text report from available Telegram data...",
    )
    _send_user_report(api, update, user, chat)


def _request_chat_id(update: dict[str, Any]) -> int | str | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    return chat_id if isinstance(chat_id, (int, str)) else None


def _notify_request_error(
    api: TelegramBotAPI,
    update: dict[str, Any],
    error: Exception,
) -> None:
    chat_id = _request_chat_id(update)
    if chat_id is None:
        LOGGER.error("Could not notify the user: update has no chat id")
        return

    update_id = update.get("update_id")
    reference = str(update_id) if isinstance(update_id, int) else "unknown"
    if isinstance(error, BotAPIError):
        detail = str(error)[:1000]
        message = (
            f"Could not process request {reference}. "
            f"Telegram API error: {detail}"
        )
    else:
        message = (
            f"Could not process request {reference} due to an internal error. "
            "Details were written to the bot console."
        )

    try:
        api.send_message(chat_id, message)
        LOGGER.info("Request error notification sent to chat")
    except Exception:
        LOGGER.exception("Could not send request error notification to chat")


def process_update(api: TelegramBotAPI, update: dict[str, Any]) -> None:
    update_id = update.get("update_id")
    reference = str(update_id) if isinstance(update_id, int) else "unknown"
    try:
        handle_update(api, update)
    except BotAPIError as error:
        LOGGER.exception("Telegram API error while processing update %s", reference)
        _notify_request_error(api, update, error)
    except Exception as error:
        LOGGER.exception("Unexpected error while processing update %s", reference)
        _notify_request_error(api, update, error)


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _poll_timeout() -> int:
    raw_value = os.environ.get("TELEGRAM_POLL_TIMEOUT", str(DEFAULT_POLL_TIMEOUT))
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_POLL_TIMEOUT
    return max(1, min(value, MAX_POLL_TIMEOUT))


def run_bot(api: TelegramBotAPI, poll_timeout: int) -> None:
    bot_user = api.call("getMe")
    username = bot_user.get("username") if isinstance(bot_user, dict) else None
    LOGGER.info("Bot @%s started", username or "unknown")

    offset: int | None = None
    retry_delay = 1
    while True:
        payload: dict[str, Any] = {
            "timeout": poll_timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset

        try:
            updates = api.call(
                "getUpdates",
                payload,
                timeout=poll_timeout + 10,
            )
            if not isinstance(updates, list):
                raise BotAPIError("getUpdates: unexpected result type")
            if updates:
                LOGGER.info("Received %s Telegram update(s)", len(updates))
            retry_delay = 1
        except BotAPIError as error:
            LOGGER.warning("%s; retrying in %s seconds", error, retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)
            continue

        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = max(offset or 0, update_id + 1)
            process_update(api, update)


def main() -> None:
    project_dir = Path(__file__).resolve().parent
    load_env_file(project_dir / ".env")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not configured. Copy .env.example to .env "
            "and add the token from @BotFather."
        )

    try:
        run_bot(TelegramBotAPI(token), _poll_timeout())
    except KeyboardInterrupt:
        LOGGER.info("Bot stopped")
    except BotAPIError as error:
        LOGGER.error("Could not start bot: %s", error)
        raise SystemExit(f"Could not start bot: {error}") from None


if __name__ == "__main__":
    main()

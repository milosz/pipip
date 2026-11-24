"""Small shared utility helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_iso8601(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def truncate(value: str | None, limit: int = 4000) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def safe_log(value: object, limit: int = 1000) -> str:
    """Escape line breaks and truncate untrusted log values."""

    text = str(value).replace("\r", r"\r").replace("\n", r"\n")
    return text if len(text) <= limit else text[: limit - 1] + "…"

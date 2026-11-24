"""Human-readable timestamps and statuses for HTML outputs."""

from __future__ import annotations

from datetime import UTC, datetime

_STATUS_LABELS = {
    "ok": "OK",
    "error": "Error",
    "no_release": "No release",
    "pending": "Pending",
    "match": "Match",
    "drift": "Drift",
    "missing": "Missing",
}


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def status_label(value: str) -> str:
    return _STATUS_LABELS.get(value, value.replace("_", " ").title())

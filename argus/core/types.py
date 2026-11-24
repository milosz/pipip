"""Shared value objects used by the Argus core, plugins and outputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass(slots=True)
class ReleaseInfo:
    """A single release/tag discovered by a source provider."""

    version: str
    url: str | None = None
    notes: str | None = None
    published_at: datetime | None = None
    prerelease: bool = False
    digest: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CheckResult:
    """Outcome of checking a single monitored application."""

    target: str
    source: str
    status: Literal["ok", "error", "no_release"]
    previous_version: str | None = None
    new_version: str | None = None
    changed: bool = False
    digest_changed: bool = False
    error: str | None = None
    checked_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(slots=True)
class RecentChange:
    """A version detection in the shared recent changes list."""

    target: str
    version: str
    previous_version: str | None
    detected_at: datetime


@dataclass(slots=True)
class Notification:
    """Payload handed to notification plugins."""

    app_name: str
    source: str
    old_version: str | None
    new_version: str
    release_url: str | None = None
    release_notes: str | None = None
    title_override: str | None = None
    message_override: str | None = None
    idempotency_key: str | None = None
    event_type: str = "version"

    @property
    def title(self) -> str:
        if self.title_override:
            return self.title_override
        if self.old_version:
            return f"{self.app_name}: new version detected"
        return f"{self.app_name}: first version detected"

    @property
    def message(self) -> str:
        if self.message_override:
            return self.message_override
        if self.old_version:
            return f"{self.old_version} → {self.new_version}"
        return f"Detected version {self.new_version}."

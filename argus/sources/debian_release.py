"""Debian distribution Release file source.

This monitors the Debian point release (for example ``12.7``) published in the
``dists/<suite>/Release`` file of an APT repository.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from argus.config import AppConfig, DebianReleaseOptions
from argus.core.plugins import register_source
from argus.core.types import ReleaseInfo
from argus.sources.base import SourceError, SourceProvider


def parse_release_fields(text: str) -> dict[str, str]:
    """Parse the leading scalar fields of a Debian ``Release`` file."""

    fields: dict[str, str] = {}
    for line in text.splitlines():
        if line[:1] in (" ", "\t"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields.setdefault(key.strip(), value.strip())
    return fields


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


@register_source("debian_release")
class DebianReleaseSource(SourceProvider):
    """Read the Debian release version from an APT ``Release`` file."""

    name = "debian_release"
    options_model = DebianReleaseOptions

    def __init__(self, options: DebianReleaseOptions, settings: AppConfig) -> None:
        super().__init__(options, settings)
        self.options: DebianReleaseOptions = options
        self.default_timeout = settings.sources.debian_release.timeout_seconds

    def _url(self) -> str:
        base = self.options.base_url.rstrip("/")
        return f"{base}/dists/{self.options.suite.strip('/')}/Release"

    async def list_releases(self) -> list[ReleaseInfo]:
        url = self._url()
        try:
            async with self.create_client() as client:
                response = await self.request(client, "GET", url)
                response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise SourceError(
                f"Debian Release file returned HTTP {error.response.status_code} for {url}"
            ) from error
        except httpx.HTTPError as error:
            raise SourceError(f"Debian Release request failed: {error}") from error

        fields = parse_release_fields(response.text)
        version = fields.get("Version", "").strip()
        if not version:
            raise SourceError(f"Debian Release file at {url} has no Version field")

        notes = "; ".join(
            f"{key}: {fields[key]}" for key in ("Codename", "Suite", "Date") if fields.get(key)
        )
        metadata = {
            key.lower(): fields[key]
            for key in (
                "Origin",
                "Label",
                "Suite",
                "Codename",
                "Version",
                "Date",
                "Architectures",
            )
            if key in fields
        }
        return [
            ReleaseInfo(
                version=version,
                url=url,
                notes=notes or None,
                published_at=_parse_date(fields.get("Date")),
                metadata=metadata,
            )
        ]

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from argus.config import DebianReleaseOptions, parse_config
from argus.sources.base import SourceError
from argus.sources.debian_release import DebianReleaseSource, _parse_date, parse_release_fields

RELEASE = """Origin: Debian
Label: Debian
Suite: stable
Codename: bookworm
Date: Sat, 06 Sep 2025 00:00:00 UTC
Architectures: amd64 arm64
Version: 12.7
Description: Debian 12.7
"""


def test_parse_release_fields_ignores_continuation_lines() -> None:
    fields = parse_release_fields(RELEASE)
    assert fields["Suite"] == "stable"
    assert fields["Codename"] == "bookworm"
    assert fields["Version"] == "12.7"
    assert fields["Architectures"] == "amd64 arm64"


def test_parse_release_date() -> None:
    fields = parse_release_fields(RELEASE)
    parsed = _parse_date(fields["Date"])
    assert parsed == datetime(2025, 9, 6, tzinfo=UTC)
    assert _parse_date(None) is None
    assert _parse_date("not a date") is None
    assert _parse_date("31 Dec 9999 23:59:59 -0100") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [RELEASE, "Version: 12.7\n"])
async def test_debian_release_fetches_metadata(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    source = DebianReleaseSource(
        DebianReleaseOptions(base_url="https://apt.test/debian/", suite="/stable/"),
        parse_config({}),
    )
    expected_url = "https://apt.test/debian/dists/stable/Release"

    async def request(client, method, url):
        assert (method, url) == ("GET", expected_url)
        return httpx.Response(200, text=content, request=httpx.Request(method, url))

    monkeypatch.setattr(source, "request", request)
    release = await source.latest()
    assert release is not None
    assert release.version == "12.7"
    assert release.url == expected_url
    if content == RELEASE:
        assert release.published_at == datetime(2025, 9, 6, tzinfo=UTC)
        assert release.notes == (
            "Codename: bookworm; Suite: stable; Date: Sat, 06 Sep 2025 00:00:00 UTC"
        )
        assert release.metadata["architectures"] == "amd64 arm64"
        assert "description" not in release.metadata
    else:
        assert release.notes is None
        assert release.published_at is None
        assert release.metadata == {"version": "12.7"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message"),
    [(404, "HTTP 404"), (None, "request failed: offline"), (200, "has no Version field")],
)
async def test_debian_release_reports_unavailable_versions(
    monkeypatch: pytest.MonkeyPatch, status: int | None, message: str
) -> None:
    source = DebianReleaseSource(DebianReleaseOptions(), parse_config({}))

    async def request(client, method, url):
        if status is None:
            raise httpx.ConnectError("offline")
        return httpx.Response(
            status, text="Suite: stable\nVersion:   \n", request=httpx.Request(method, url)
        )

    monkeypatch.setattr(source, "request", request)
    with pytest.raises(SourceError, match=message):
        await source.latest()


def test_debian_release_parser_keeps_first_header_and_ignores_checksum_entries() -> None:
    fields = parse_release_fields("Version: 12.7\nVersion: ignored\nSHA256:\n abc: def\ngarbage")
    assert fields == {"Version": "12.7", "SHA256": ""}
    assert _parse_date("Sat, 06 Sep 2025 00:00:00 -0000") == datetime(2025, 9, 6, tzinfo=UTC)

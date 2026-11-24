from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from argus.config import CodebergOptions, GitHubOptions, parse_config
from argus.sources.base import SourceError
from argus.sources.codeberg import CodebergSource
from argus.sources.github import GitHubSource


@pytest.mark.asyncio
@pytest.mark.parametrize("forge", ["github", "codeberg"])
async def test_forge_paginates_and_prefers_release_metadata(
    forge: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = parse_config({})
    if forge == "github":
        source = GitHubSource(GitHubOptions(repo="org/demo", token="demo-token"), settings)
        base = "https://api.github.com"
        auth = "Bearer demo-token"
        size = 100
        size_key = "per_page"
    else:
        source = CodebergSource(
            CodebergOptions(repo="org/demo", token="demo-token", base_url="https://forge.test/"),
            settings,
        )
        base = "https://forge.test/api/v1"
        auth = "token demo-token"
        size = 50
        size_key = "limit"
    calls = []

    async def request(client, method, url, *, params):
        assert method == "GET"
        assert client.headers["Authorization"] == auth
        assert url.startswith(f"{base}/repos/org/demo/")
        assert params[size_key] == size
        path = url.rsplit("/", 1)[-1]
        page = params["page"]
        calls.append((path, page))
        if path == "tags":
            items = [{"name": "v2.0.0"}, {"name": "v1.1.0", "commit": {"sha": "abc"}}, {}]
        elif page == 1:
            items = [{"tag_name": "v9.0.0", "draft": True}] * size
        else:
            items = [
                {"tag_name": "v3.0.0-rc.1", "prerelease": True},
                {
                    "tag_name": "v2.0.0",
                    "html_url": "https://forge.test/release",
                    "body": "Changelog",
                    "published_at": "2026-01-01T01:00:00+01:00",
                    "author": {"login": "maintainer"},
                    "assets": [{"name": "artifact", "size": 21}],
                },
                {},
            ]
        return httpx.Response(200, request=httpx.Request(method, url), json=items)

    monkeypatch.setattr(source, "request", request)
    releases = {release.version: release for release in await source.list_releases()}
    assert calls == [("releases", 1), ("releases", 2), ("tags", 1)]
    assert set(releases) == {"v1.1.0", "v2.0.0", "v3.0.0-rc.1"}
    assert releases["v1.1.0"].digest == "abc"
    release = releases["v2.0.0"]
    assert release.url == "https://forge.test/release"
    assert release.notes == "Changelog"
    assert release.published_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert release.metadata["author"] == "maintainer"
    if forge == "github":
        assert release.metadata["assets"] == [{"name": "artifact", "size": 21}]
    latest = await source.latest()
    assert latest is not None and latest.version == "v2.0.0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message"),
    [(404, "repository not found"), (503, "HTTP 503"), (200, "unexpected"),
     (None, "request failed")],
)
@pytest.mark.parametrize("forge", ["github", "codeberg"])
async def test_forge_reports_bad_responses(
    status: int | None, message: str, forge: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = parse_config({})
    source = (
        GitHubSource(GitHubOptions(repo="org/demo"), settings) if forge == "github"
        else CodebergSource(CodebergOptions(repo="org/demo"), settings)
    )

    async def request(client, method, url, **kwargs):
        if status is None:
            raise httpx.ConnectError("offline")
        return httpx.Response(status, request=httpx.Request(method, url), json={})

    monkeypatch.setattr(source, "request", request)
    with pytest.raises(SourceError, match=message):
        await source.list_releases()


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (None, "token codeberg-token"),
        ("https://codeberg.org/", "token codeberg-token"),
        ("https://code.forgejo.org", None),
    ],
)
def test_codeberg_only_reuses_global_token_at_its_configured_base_url(
    base_url: str | None, expected: str | None
) -> None:
    settings = parse_config({"sources": {"codeberg": {"token": "codeberg-token"}}})
    source = CodebergSource(CodebergOptions(repo="forgejo/runner", base_url=base_url), settings)

    assert source.headers.get("Authorization") == expected

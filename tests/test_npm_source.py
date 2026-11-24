from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from pipip.config import NpmOptions, parse_config
from pipip.sources.base import SourceError
from pipip.sources.npm import NpmSource


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dist_tag", "include_prerelease", "expected"),
    [
        (None, False, "2.0.0"),
        (None, True, "2.1.0-beta.1"),
        ("latest", False, "1.5.0"),
        ("next", False, None),
        ("next", True, "2.1.0-beta.1"),
    ],
)
async def test_npm_monitors_scoped_packages(
    monkeypatch: pytest.MonkeyPatch, dist_tag: str | None,
    include_prerelease: bool, expected: str | None
) -> None:
    source = NpmSource(
        NpmOptions(package="@scope/demo", include_prerelease=include_prerelease, dist_tag=dist_tag),
        parse_config({}),
    )

    async def request(client, method, url):  # type: ignore[no-untyped-def]
        assert method == "GET"
        assert url == "https://registry.npmjs.org/@scope%2Fdemo"
        return httpx.Response(
            200,
            request=httpx.Request(method, url),
            json={
                "dist-tags": {"latest": "1.5.0", "next": "2.1.0-beta.1"},
                "versions": {
                    "invalid": {},
                    "1.5.0": {
                        "dist": {"integrity": "sha512-example", "unpackedSize": 4096},
                        "engines": {"node": ">=18", "npm": ">=9"},
                        "license": "MIT",
                        "description": "An example package",
                        "repository": {"url": "git+https://example.test/demo.git"},
                        "deprecated": "Use v2",
                        "dependencies": {"runtime": "^1.0.0"},
                        "optionalDependencies": {"optional": "^2.0.0"},
                        "peerDependencies": {"peer": "^3.0.0"},
                        "devDependencies": {"test": "^4.0.0"},
                    },
                    "2.0.0": {},
                    "2.1.0-beta.1": {"engines": {"node": 22}},
                },
                "time": {"1.5.0": "2026-01-01T00:00:00Z"},
            },
        )

    monkeypatch.setattr(source, "request", request)
    release = await source.latest()
    if expected is None:
        assert release is None
        return
    assert release is not None
    assert release.version == expected
    assert release.url == f"https://www.npmjs.com/package/@scope/demo/v/{expected}"
    if dist_tag == "latest":
        assert release.published_at == datetime(2026, 1, 1, tzinfo=UTC)
        assert release.digest == "sha512-example"
        assert release.metadata == {
            "requires_node": ">=18", "requires_npm": ">=9", "license": "MIT",
            "description": "An example package", "repository": "git+https://example.test/demo.git",
            "deprecated": "Use v2", "unpacked_size_bytes": 4096,
            "dependencies": {"runtime": "^1.0.0"}, "optional_dependencies": {"optional": "^2.0.0"},
            "peer_dependencies": {"peer": "^3.0.0"}, "dev_dependencies": {"test": "^4.0.0"},
        }
    else:
        assert release.metadata == {}


@pytest.mark.asyncio
async def test_npm_reports_unknown_dist_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    options = NpmOptions(package="demo", dist_tag="unknown")
    source = NpmSource(options, parse_config({}))

    async def request(client, method, url):  # type: ignore[no-untyped-def]
        return httpx.Response(200, request=httpx.Request(method, url), json={"versions": {}})

    monkeypatch.setattr(source, "request", request)
    with pytest.raises(SourceError, match="dist-tag 'unknown' not found"):
        await source.latest()


@pytest.mark.asyncio
async def test_npm_private_registry_omits_release_url(monkeypatch: pytest.MonkeyPatch) -> None:
    source = NpmSource(
        NpmOptions(package="@scope/private", base_url="https://mirror.test/npm/"),
        parse_config({}),
    )

    async def request_json(url: str) -> dict:
        assert url == "https://mirror.test/npm/@scope%2Fprivate"
        return {"versions": {"1.2.0": {}}}

    monkeypatch.setattr(source, "request_json", request_json)
    release = await source.latest()
    assert release is not None
    assert release.url is None

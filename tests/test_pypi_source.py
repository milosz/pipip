from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from pipip.config import PyPIOptions, parse_config
from pipip.sources.base import SourceError
from pipip.sources.pypi import PyPISource


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_prerelease", "expected"), [(False, "1.2.0.post1"), (True, "1.3.0rc1")]
)
async def test_pypi_uses_pep440_and_ignores_yanked_files(
    monkeypatch: pytest.MonkeyPatch, include_prerelease: bool, expected: str
) -> None:
    source = PyPISource(
        PyPIOptions(package="Demo_Package", include_prerelease=include_prerelease),
        parse_config({}),
    )

    async def request(client, method, url):  # type: ignore[no-untyped-def]
        assert method == "GET"
        assert url == "https://pypi.org/simple/demo-package/"
        assert client.headers["Accept"] == "application/vnd.pypi.simple.v1+json"
        return httpx.Response(
            200,
            request=httpx.Request(method, url),
            json={"files": [
                {"filename": "demo_package-1.2.0.tar.gz"},
                {"filename": "demo_package-1.2.0.post1.tar.gz",
                 "upload-time": "2026-01-02T00:00:00Z", "requires-python": ">=3.9"},
                {"filename": "demo_package-1.2.0.post1-py3-none-any.whl",
                 "upload-time": "2026-01-01T00:00:00Z", "requires-python": ">=3.10",
                 "size": 1200, "hashes": {"sha256": "example"},
                 "provenance": "https://example.test/provenance"},
                {"filename": "demo_package-1.2.0.post1-py2-none-any.whl",
                 "requires-python": ">=3.9"},
                {"filename": "demo_package-1.2.0.post1-py2.py3-none-any.whl",
                 "requires-python": ">=3.5", "yanked": True},
                {"filename": "demo_package-1.3.0rc1.tar.gz", "requires-python": ">=3.12"},
                {"filename": "demo_package-1.3.0.tar.gz", "yanked": "bad release"},
                {"filename": "unknown.zip"},
            ]},
        )

    monkeypatch.setattr(source, "request", request)
    release = await source.latest()
    assert release is not None
    assert release.version == expected
    assert release.url == f"https://pypi.org/project/demo-package/{expected}/"
    assert release.prerelease == include_prerelease
    if not include_prerelease:
        assert release.published_at == datetime(2026, 1, 1, tzinfo=UTC)
        assert release.metadata["requires_python"] == [">=3.10", ">=3.9"]
        assert release.metadata["wheels"] == 2
        assert release.metadata["source_archives"] == 1
        files = {item["filename"]: item for item in release.metadata["files"]}
        assert files["demo_package-1.2.0.post1-py3-none-any.whl"] == {
            "filename": "demo_package-1.2.0.post1-py3-none-any.whl",
            "type": "wheel",
            "size_bytes": 1200,
            "hashes": {"sha256": "example"},
            "provenance": "https://example.test/provenance",
            "requires_python": ">=3.10",
        }
        assert files["demo_package-1.2.0.post1-py2.py3-none-any.whl"]["yanked"] is True
    else:
        assert release.metadata["requires_python"] == ">=3.12"
        assert release.metadata["source_archives"] == 1


@pytest.mark.asyncio
async def test_pypi_reports_missing_project(monkeypatch: pytest.MonkeyPatch) -> None:
    source = PyPISource(PyPIOptions(package="missing"), parse_config({}))

    async def request(client, method, url):  # type: ignore[no-untyped-def]
        return httpx.Response(404, request=httpx.Request(method, url))

    monkeypatch.setattr(source, "request", request)
    with pytest.raises(SourceError, match="404"):
        await source.latest()


@pytest.mark.asyncio
async def test_pypi_private_index_omits_public_release_url(monkeypatch: pytest.MonkeyPatch) -> None:
    source = PyPISource(
        PyPIOptions(package="private_package", base_url="https://mirror.test/python/simple/"),
        parse_config({}),
    )

    async def request_json(url: str, *, accept: str) -> dict:
        assert url == "https://mirror.test/python/simple/private-package/"
        return {"files": [{"filename": "private_package-1.2.0.tar.gz"}]}

    monkeypatch.setattr(source, "request_json", request_json)
    release = await source.latest()
    assert release is not None
    assert release.url is None
    assert release.metadata == {
        "wheels": 0, "source_archives": 1,
        "files": [{"filename": "private_package-1.2.0.tar.gz", "type": "source archive"}],
    }

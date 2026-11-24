from __future__ import annotations

import gzip
import lzma

import httpx
import pytest

from pipip.config import AptRepoOptions, parse_config
from pipip.sources.apt_repo import AptRepoSource
from pipip.sources.base import SourceError

_INDEX = b"""Package: other
Version: 99
ignored line

Package: nginx

Package: nginx
Version: 1.2.0-1

Package: nginx
Version: 1:1.0.0-2
Architecture: amd64
Description: Short line
 continued here
SHA256: demo-digest
Filename: pool/nginx.deb

Package: nginx
Version: 1:1.0.0-2
Description: Duplicate
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "xz", "plain"])
async def test_apt_reads_package_stanzas_and_falls_back(
    encoding: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = AptRepoSource(
        AptRepoOptions(package="nginx", base_url="https://apt.test/", suite="stable"),
        parse_config({}),
    )
    urls = []

    async def request(client, method, url):
        urls.append(url)
        if url.endswith(".gz"):
            content = gzip.compress(_INDEX) if encoding == "gzip" else b"\x1f\x8b"
            return httpx.Response(200, content=content)
        if url.endswith(".xz"):
            if encoding == "xz":
                return httpx.Response(200, content=lzma.compress(_INDEX))
            return httpx.Response(404)
        return httpx.Response(200, content=_INDEX)

    monkeypatch.setattr(source, "request", request)
    latest = await source.latest()
    prefix = "https://apt.test/dists/stable/main/binary-amd64/Packages"
    candidates = [prefix + ".gz", prefix + ".xz", prefix]
    assert urls == candidates[: ["gzip", "xz", "plain"].index(encoding) + 1]
    assert latest is not None and latest.version == "1:1.0.0-2"
    assert latest.notes == "Short line\ncontinued here"
    assert latest.digest == "demo-digest"
    assert latest.metadata["filename"] == "pool/nginx.deb"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 503, None])
async def test_apt_explicit_index_and_errors(
    status: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = AptRepoSource(
        AptRepoOptions(
            package="missing", base_url="https://apt.test",
            suite="stable", packages_url="https://apt.test/custom",
        ),
        parse_config({}),
    )

    async def request(client, method, url):
        assert url == "https://apt.test/custom"
        if status is None:
            raise httpx.ConnectError("offline")
        return httpx.Response(status, content=_INDEX)

    monkeypatch.setattr(source, "request", request)
    if status == 200:
        assert await source.latest() is None
    else:
        with pytest.raises(SourceError, match="offline" if status is None else "HTTP 503"):
            await source.latest()

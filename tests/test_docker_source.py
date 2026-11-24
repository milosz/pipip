from __future__ import annotations

import httpx
import pytest

from argus.config import DockerOptions, parse_config
from argus.core.versioning import select_latest
from argus.sources.base import SourceError
from argus.sources.docker import DockerSource


@pytest.mark.asyncio
async def test_docker_reads_all_tag_pages_before_selecting_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = DockerOptions(image="nginx", max_candidates=1, max_pages=2)
    source = DockerSource(options, parse_config({}))
    cursors: list[str | None] = []

    async def request(client, method, url, *, params):
        cursor = params.get("last")
        cursors.append(cursor)
        if cursor is None:
            return httpx.Response(
                200,
                request=httpx.Request(method, url),
                json={"tags": ["1.0.0"]},
                headers={"Link": '<?last=1.0.0>; rel="next"'},
            )
        return httpx.Response(
            200, request=httpx.Request(method, url), json={"tags": ["2.0.0"]}
        )

    monkeypatch.setattr(source, "request", request)
    releases = await source.list_releases()
    latest = select_latest(releases, options)

    assert cursors == [None, "1.0.0"]
    assert latest is not None
    assert latest.version == "2.0.0"


@pytest.mark.asyncio
async def test_docker_rejects_malformed_tag_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    source = DockerSource(DockerOptions(image="nginx"), parse_config({}))

    async def request(client, method, url, **kwargs):
        return httpx.Response(
            200, request=httpx.Request(method, url), json={"tags": "invalid"}
        )

    monkeypatch.setattr(source, "request", request)
    with pytest.raises(SourceError, match="invalid tag listing"):
        await source.list_releases()


@pytest.mark.asyncio
async def test_docker_bearer_challenge_and_multi_platform_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = DockerSource(
        DockerOptions(image="registry.test:5000/org/demo:ignored"),
        parse_config({}),
    )
    base = "https://registry.test:5000/v2/org/demo"
    realm = "https://auth.test/token"
    challenge = f'Bearer realm="{realm}",service="registry.test"'
    manifest_url = f"{base}/manifests/1.2.0"

    async def request(client, method, url, *, params=None, headers=None, auth=None):
        assert method == "GET"
        raw = httpx.Request(method, url)
        if url == realm:
            assert params == {"service": "registry.test", "scope": "repository:org/demo:pull"}
            return httpx.Response(200, request=raw, json={"access_token": "issued-token"})
        authorization = (headers or {}).get("Authorization") or client.headers.get("Authorization")
        if not authorization:
            return httpx.Response(401, request=raw, headers={"WWW-Authenticate": challenge})
        assert authorization == "Bearer issued-token"
        if url == f"{base}/tags/list":
            return httpx.Response(200, request=raw, json={"tags": ["1.2.0", "2.0.0-rc.1"]})
        assert url == manifest_url
        assert "application/vnd.oci.image.index.v1+json" in headers["Accept"]
        return httpx.Response(
            200, request=raw, headers={"Docker-Content-Digest": "sha256:index"},
            json={"manifests": [
                {"digest": "sha256:arm", "platform": {
                    "os": "linux", "architecture": "arm", "variant": "v7",
                }},
                {"digest": "sha256:attestation", "platform": {
                    "os": "unknown", "architecture": "unknown",
                }},
            ]},
        )

    monkeypatch.setattr(source, "request", request)
    release = await source.latest()
    assert release is not None and release.version == "1.2.0"
    assert release.digest == "sha256:index"
    assert release.metadata["platforms"] == [
        {"digest": "sha256:arm", "os": "linux", "architecture": "arm", "variant": "v7"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_status", [200, 503])
async def test_docker_single_platform_metadata_and_manifest_failure(
    manifest_status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = DockerSource(
        DockerOptions(image="nginx", username="robot", password="demo-secret"),
        parse_config({}),
    )
    base = "https://registry-1.docker.io/v2/library/nginx"

    async def request(client, method, url, *, params=None, headers=None):
        assert ((headers or {}).get("Authorization") or client.headers.get("Authorization")
                ) == "Basic cm9ib3Q6ZGVtby1zZWNyZXQ="
        raw = httpx.Request(method, url)
        if url == f"{base}/tags/list":
            return httpx.Response(200, request=raw, json={"tags": ["1.2.0"]})
        if url == f"{base}/manifests/1.2.0":
            return httpx.Response(manifest_status, request=raw, json={
                "config": {"size": 5, "digest": "sha256:config"}, "layers": [{"size": 10}],
            })
        assert url == f"{base}/blobs/sha256:config"
        return httpx.Response(200, request=raw, json={
            "architecture": "amd64", "os": "linux", "config": {"Labels": {"demo": "value"}},
        })

    monkeypatch.setattr(source, "request", request)
    release = await source.latest()
    assert release is not None and release.version == "1.2.0"
    assert release.url == "https://hub.docker.com/_/nginx/tags?name=1.2.0"
    if manifest_status == 200:
        assert release.metadata["size"] == 15
        assert release.metadata["config_digest"] == "sha256:config"
        assert release.metadata["labels"] == {"demo": "value"}
    else:
        assert release.digest is None
        assert "config_digest" not in release.metadata

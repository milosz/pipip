"""Docker Hub and OCI registry v2 tag source."""

from __future__ import annotations

import logging
import re
from typing import ClassVar
from urllib.parse import quote

import httpx

from pipip.config import AppConfig, DockerOptions, SourceOptions
from pipip.core.plugins import register_source
from pipip.core.types import ReleaseInfo
from pipip.sources.base import SourceError, SourceProvider

logger = logging.getLogger(__name__)

_AUTH_PARAM_RE = re.compile(r'(\w+)="([^"]*)"')
_DOCKER_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io"}


def _split_image(image: str, default_registry: str) -> tuple[str, str]:
    value = image.strip()
    if "@" in value:
        value = value.split("@", 1)[0]
    last_segment = value.rsplit("/", 1)[-1]
    if ":" in last_segment:
        value = value.rsplit(":", 1)[0]

    parts = value.split("/")
    if len(parts) > 1 and (
        "." in parts[0] or ":" in parts[0] or parts[0] == "localhost"
    ):
        registry = parts[0]
        repository = "/".join(parts[1:])
    else:
        registry = default_registry
        repository = value

    if registry in _DOCKER_HUB_ALIASES:
        registry = "registry-1.docker.io"
        if "/" not in repository:
            repository = f"library/{repository}"
    return registry, repository


@register_source("docker")
class DockerSource(SourceProvider):
    """Fetch tags from an OCI/Docker registry using the v2 API."""

    name = "docker"
    options_model: ClassVar[type[SourceOptions]] = DockerOptions

    def __init__(self, options: DockerOptions, settings: AppConfig) -> None:
        super().__init__(options, settings)
        self.options: DockerOptions = options
        self.default_timeout = settings.sources.docker.timeout_seconds
        registry = options.registry or "registry-1.docker.io"
        registry = registry.removeprefix("https://").removeprefix("http://").rstrip("/")
        self.registry, self.repository = _split_image(options.image, registry)

    def _base_url(self) -> str:
        return f"{self.options.scheme}://{self.registry}/v2/{self.repository}/tags/list"

    def _initial_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.options.token:
            headers["Authorization"] = f"Bearer {self.options.token.get_secret_value()}"
        elif self.options.username and self.options.password:
            import base64

            credentials = (
                f"{self.options.username}:{self.options.password.get_secret_value()}"
            ).encode()
            headers["Authorization"] = f"Basic {base64.b64encode(credentials).decode()}"
        return headers

    async def _fetch_bearer_token(
        self,
        client: httpx.AsyncClient,
        challenge: str,
        repository: str,
    ) -> str:
        params = dict(_AUTH_PARAM_RE.findall(challenge))
        realm = params.get("realm")
        if not realm:
            raise SourceError("registry returned an unsupported auth challenge")
        query: dict[str, str] = {}
        if params.get("service"):
            query["service"] = params["service"]
        query["scope"] = params.get("scope") or f"repository:{repository}:pull"
        auth: httpx.BasicAuth | None = None
        if self.options.username and self.options.password:
            auth = httpx.BasicAuth(
                self.options.username, self.options.password.get_secret_value()
            )
        response = await self.request(client, "GET", realm, params=query, auth=auth)
        if response.status_code >= 400:
            raise SourceError(
                f"registry token request failed with HTTP {response.status_code}"
            )
        payload = response.json()
        token = payload.get("token") or payload.get("access_token")
        if not token:
            raise SourceError("registry token response did not contain a token")
        return str(token)

    async def _authorized_get(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        request_headers = dict(headers or {})
        response = await self.request(client, "GET", url, headers=request_headers, params=params)
        if response.status_code != 401:
            return response
        challenge = response.headers.get("www-authenticate", "")
        scheme = challenge.split(" ", 1)[0].lower()
        if scheme == "basic":
            if self.options.username and self.options.password:
                raise SourceError(
                    f"registry {self.registry} rejected basic credentials"
                )
            raise SourceError(
                f"registry {self.registry} requires Basic Auth; "
                "configure username and password"
            )
        if scheme and scheme != "bearer":
            raise SourceError(
                f"registry {self.registry} returned unsupported "
                f"authentication scheme {scheme!r}"
            )
        token = await self._fetch_bearer_token(client, challenge, self.repository)
        retry_headers = dict(request_headers)
        retry_headers["Authorization"] = f"Bearer {token}"
        return await self.request(client, "GET", url, headers=retry_headers, params=params)

    async def _manifest_metadata(
        self, client: httpx.AsyncClient, tag: str
    ) -> tuple[str | None, dict[str, object]]:
        base = f"{self.options.scheme}://{self.registry}/v2/{self.repository}"
        accept = ", ".join(
            (
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            )
        )
        headers = self._initial_headers()
        headers["Accept"] = accept
        response = await self._authorized_get(
            client,
            f"{base}/manifests/{quote(tag, safe='')}",
            headers=headers,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        digest = response.headers.get("docker-content-digest")
        metadata: dict[str, object] = {
            "media_type": payload.get("mediaType")
            or response.headers.get("content-type"),
        }
        if digest:
            metadata["manifest_digest"] = digest

        manifests = payload.get("manifests")
        if isinstance(manifests, list):
            platforms: list[dict[str, object]] = []
            for entry in manifests:
                platform = entry.get("platform") or {}
                if platform.get("os") == "unknown" or platform.get(
                    "architecture"
                ) == "unknown":
                    continue
                info = {
                    "digest": entry.get("digest"),
                    "architecture": platform.get("architecture"),
                    "os": platform.get("os"),
                    "variant": platform.get("variant"),
                }
                platforms.append(
                    {key: value for key, value in info.items() if value}
                )
            if platforms:
                metadata["platforms"] = platforms
        else:
            config = payload.get("config") or {}
            config_digest = config.get("digest")
            layers = payload.get("layers") or []
            if config_digest:
                metadata["config_digest"] = config_digest
            if layers:
                metadata["layers"] = len(layers)
            sizes = [config.get("size")] + [layer.get("size") for layer in layers]
            total = sum(size for size in sizes if isinstance(size, int))
            if total:
                metadata["size"] = total
            if config_digest:
                try:
                    blob = await self._authorized_get(
                        client,
                        f"{base}/blobs/{config_digest}",
                        headers=self._initial_headers(),
                    )
                    blob.raise_for_status()
                    config_payload = blob.json()
                except (httpx.HTTPError, ValueError):
                    config_payload = {}
                if config_payload:
                    created = config_payload.get("created")
                    if created:
                        metadata["created"] = created
                    architecture = config_payload.get("architecture")
                    if architecture:
                        metadata["architecture"] = architecture
                    os_name = config_payload.get("os")
                    if os_name:
                        metadata["os"] = os_name
                    labels = (config_payload.get("config") or {}).get("Labels")
                    if labels:
                        metadata["labels"] = labels

        return digest, {
            key: value
            for key, value in metadata.items()
            if value not in (None, "", [], {})
        }

    async def latest(self) -> ReleaseInfo | None:
        release = await super().latest()
        if release is None:
            return None
        try:
            async with self.create_client() as client:
                digest, metadata = await self._manifest_metadata(
                    client, release.version
                )
        except (httpx.HTTPError, SourceError, ValueError) as error:
            logger.warning(
                "manifest metadata for %s:%s unavailable: %s",
                self.repository,
                release.version,
                error,
            )
            return release
        if digest:
            release.digest = digest
        release.metadata.update(metadata)
        return release

    async def list_releases(self) -> list[ReleaseInfo]:
        releases: dict[str, ReleaseInfo] = {}
        last: str | None = None
        headers = self._initial_headers()
        page_size = 100
        url = self._base_url()
        async with self.create_client(headers=headers) as client:
            for _ in range(self.options.max_pages):
                params: dict[str, str | int] = {"n": page_size}
                if last:
                    params["last"] = last
                try:
                    response = await self.request(client, "GET", url, params=params)
                    if response.status_code == 401:
                        challenge = response.headers.get("www-authenticate", "")
                        scheme = challenge.split(" ", 1)[0].lower()
                        if scheme == "basic":
                            if self.options.username and self.options.password:
                                raise SourceError(
                                    f"registry {self.registry} rejected basic "
                                    "credentials"
                                )
                            raise SourceError(
                                f"registry {self.registry} requires Basic Auth; "
                                "configure username and password"
                            )
                        if scheme and scheme != "bearer":
                            raise SourceError(
                                f"registry {self.registry} returned unsupported "
                                f"authentication scheme {scheme!r}"
                            )
                        token = await self._fetch_bearer_token(
                            client, challenge, self.repository
                        )
                        headers["Authorization"] = f"Bearer {token}"
                        client.headers.update(headers)
                        response = await self.request(
                            client, "GET", url, params=params
                        )
                    response.raise_for_status()
                except httpx.HTTPStatusError as error:
                    raise SourceError(
                        f"registry {self.registry} returned HTTP "
                        f"{error.response.status_code} for {self.repository}"
                    ) from error
                except httpx.HTTPError as error:
                    raise SourceError(f"registry request failed: {error}") from error

                try:
                    payload = response.json()
                except ValueError as error:
                    raise SourceError(
                        f"registry {self.registry} returned invalid tag JSON"
                    ) from error
                if not isinstance(payload, dict):
                    raise SourceError(f"registry {self.registry} returned an invalid tag listing")
                tags = payload.get("tags")
                if tags is None or tags == []:
                    break
                if not isinstance(tags, list) or any(
                    not isinstance(tag, str) or not tag for tag in tags
                ):
                    raise SourceError(f"registry {self.registry} returned an invalid tag listing")
                for tag in tags:
                    if tag in releases:
                        continue
                    releases[tag] = ReleaseInfo(
                        version=tag,
                        url=self._tag_url(tag),
                        metadata={
                            "registry": self.registry,
                            "repository": self.repository,
                            "tag": tag,
                        },
                    )
                if len(tags) < page_size and "next" not in response.links:
                    break
                if last == tags[-1]:
                    break
                last = tags[-1]
        return list(releases.values())

    def _tag_url(self, tag: str) -> str:
        if self.registry in _DOCKER_HUB_ALIASES:
            if self.repository.startswith("library/"):
                image = self.repository.removeprefix("library/")
                return f"https://hub.docker.com/_/{image}/tags?name={quote(tag)}"
            return f"https://hub.docker.com/r/{self.repository}/tags?name={quote(tag)}"
        return f"{self.options.scheme}://{self.registry}/{self.repository}/tags?name={quote(tag)}"

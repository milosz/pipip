"""Codeberg / Forgejo / Gitea Releases and Tags source."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from pipip.config import AppConfig, CodebergOptions
from pipip.core.plugins import register_source
from pipip.core.types import ReleaseInfo
from pipip.core.utils import parse_iso8601
from pipip.sources.base import SourceError, SourceProvider

logger = logging.getLogger(__name__)


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "", [])}


@register_source("codeberg")
class CodebergSource(SourceProvider):
    """Fetch releases and tags from a Gitea-compatible API."""

    name = "codeberg"
    options_model = CodebergOptions
    page_size = 50

    def __init__(self, options: CodebergOptions, settings: AppConfig) -> None:
        super().__init__(options, settings)
        self.options: CodebergOptions = options
        self.default_timeout = settings.sources.codeberg.timeout_seconds
        default_base_url = settings.sources.codeberg.base_url.rstrip("/")
        self.base_url = (options.base_url or default_base_url).rstrip("/")
        token = options.token
        if token is None and self.base_url == default_base_url:
            token = settings.sources.codeberg.token
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "pi-pip",
        }
        if token:
            self.headers["Authorization"] = f"token {token.get_secret_value()}"

    async def _fetch_paginated(
        self, client: httpx.AsyncClient, path: str
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        url = f"{self.base_url}/api/v1/repos/{self.options.repo}{path}"
        for page in range(1, self.options.max_pages + 1):
            try:
                response = await self.request(
                    client,
                    "GET",
                    url,
                    params={"page": page, "limit": self.page_size},
                )
            except httpx.HTTPError as error:
                raise SourceError(f"Codeberg request failed: {error}") from error
            if response.status_code == 404:
                raise SourceError(f"Codeberg repository not found: {self.options.repo}")
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise SourceError(
                    f"Codeberg API returned HTTP {error.response.status_code}"
                ) from error
            payload = response.json()
            if not isinstance(payload, list):
                raise SourceError("unexpected Codeberg API response")
            items.extend(payload)
            if len(payload) < self.page_size:
                break
        return items

    async def list_releases(self) -> list[ReleaseInfo]:
        releases: dict[str, ReleaseInfo] = {}
        async with self.create_client(headers=self.headers) as client:
            if self.options.use_releases:
                for item in await self._fetch_paginated(client, "/releases"):
                    if item.get("draft"):
                        continue
                    version = str(item.get("tag_name") or item.get("name") or "").strip()
                    if not version or version in releases:
                        continue
                    releases[version] = ReleaseInfo(
                        version=version,
                        url=item.get("html_url"),
                        notes=item.get("body"),
                        published_at=parse_iso8601(item.get("published_at")),
                        prerelease=bool(item.get("prerelease")),
                        metadata=_compact(
                            {
                                "tag_name": item.get("tag_name"),
                                "release_name": item.get("name"),
                                "prerelease": bool(item.get("prerelease")),
                                "draft": bool(item.get("draft")),
                                "author": (item.get("author") or {}).get(
                                    "login"
                                )
                                or (item.get("author") or {}).get("username"),
                                "target_commitish": item.get("target_commitish"),
                            }
                        ),
                    )
            if self.options.use_tags:
                for item in await self._fetch_paginated(client, "/tags"):
                    version = str(item.get("name") or "").strip()
                    if not version or version in releases:
                        continue
                    sha = (item.get("commit") or {}).get("sha")
                    releases[version] = ReleaseInfo(
                        version=version,
                        url=(
                            f"{self.base_url}/{self.options.repo}"
                            f"/releases/tag/{version}"
                        ),
                        digest=sha,
                        metadata=_compact({"tag": version, "commit_sha": sha}),
                    )
        return list(releases.values())

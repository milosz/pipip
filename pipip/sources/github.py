"""GitHub Releases and Tags source."""

from __future__ import annotations

from typing import Any

import httpx

from pipip.config import AppConfig, GitHubOptions
from pipip.core.plugins import register_source
from pipip.core.types import ReleaseInfo
from pipip.core.utils import parse_iso8601
from pipip.sources.base import SourceError, SourceProvider


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "", [])}


@register_source("github")
class GitHubSource(SourceProvider):
    """Fetch release/tag metadata from the GitHub REST API."""

    name = "github"
    options_model = GitHubOptions

    def __init__(self, options: GitHubOptions, settings: AppConfig) -> None:
        super().__init__(options, settings)
        self.options: GitHubOptions = options
        self.default_timeout = settings.sources.github.timeout_seconds
        self.api_url = settings.sources.github.api_url.rstrip("/")
        token = options.token or settings.sources.github.token
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pi-pip",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token.get_secret_value()}"

    async def _get(
        self, client: httpx.AsyncClient, url: str, page: int
    ) -> list[dict[str, Any]]:
        try:
            response = await self.request(
                client,
                "GET",
                url,
                params={"per_page": 100, "page": page},
            )
        except httpx.HTTPError as error:
            raise SourceError(f"GitHub request failed: {error}") from error
        if response.status_code == 404:
            raise SourceError(f"GitHub repository not found: {self.options.repo}")
        if response.status_code in (403, 429):
            remaining = response.headers.get("x-ratelimit-remaining")
            raise SourceError(
                "GitHub API rate limit or access denied "
                f"(HTTP {response.status_code}, remaining={remaining}); "
                "configure sources.github.token"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise SourceError(
                f"GitHub API returned HTTP {error.response.status_code}"
            ) from error
        payload = response.json()
        if not isinstance(payload, list):
            raise SourceError("unexpected GitHub API response")
        return payload

    async def _fetch_paginated(
        self, client: httpx.AsyncClient, path: str
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        url = f"{self.api_url}/repos/{self.options.repo}{path}"
        for page in range(1, self.options.max_pages + 1):
            batch = await self._get(client, url, page)
            items.extend(batch)
            if len(batch) < 100:
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
                                "author": (item.get("author") or {}).get("login"),
                                "target_commitish": item.get("target_commitish"),
                                "assets": [
                                    _compact(
                                        {
                                            "name": asset.get("name"),
                                            "size": asset.get("size"),
                                            "digest": asset.get("digest"),
                                            "url": asset.get(
                                                "browser_download_url"
                                            ),
                                        }
                                    )
                                    for asset in (item.get("assets") or [])[:20]
                                ],
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
                        url=f"https://github.com/{self.options.repo}/releases/tag/{version}",
                        digest=sha,
                        metadata=_compact({"tag": version, "commit_sha": sha}),
                    )
        return list(releases.values())

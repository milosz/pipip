"""Abstract source provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import httpx

from pipip.config import AppConfig, SourceOptions
from pipip.core.retry import request_with_retry
from pipip.core.types import ReleaseInfo
from pipip.core.versioning import select_latest


class SourceError(RuntimeError):
    """Raised when a source cannot retrieve or parse release information."""


class SourceProvider(ABC):
    """Base class for all release source plugins."""

    name: ClassVar[str]
    options_model: ClassVar[type[SourceOptions]] = SourceOptions
    default_timeout: float = 30.0

    def __init__(self, options: SourceOptions, settings: AppConfig) -> None:
        self.options = options
        self.settings = settings

    @property
    def timeout(self) -> float:
        return self.options.timeout_seconds or self.default_timeout

    def create_client(
        self,
        *,
        headers: dict[str, str] | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers=headers or {},
            follow_redirects=True,
            verify=True,
        )

    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        return await request_with_retry(
            client,
            method,
            url,
            retry=self.settings.retry,
            allow_private=self.settings.security.allow_private_networks,
            **kwargs,
        )

    async def request_json(
        self, url: str, *, accept: str = "application/json"
    ) -> dict[str, Any]:
        async with self.create_client(
            headers={"Accept": accept, "User-Agent": "pi-pip"}
        ) as client:
            try:
                response = await self.request(client, "GET", url)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise SourceError(f"{self.name} request failed: {error}") from error
        if not isinstance(payload, dict):
            raise SourceError(f"unexpected {self.name} JSON response")
        return payload

    @abstractmethod
    async def list_releases(self) -> list[ReleaseInfo]:
        """Return all candidate releases (unfiltered and unsorted)."""

    async def latest(self) -> ReleaseInfo | None:
        releases = await self.list_releases()
        return select_latest(releases, self.options)

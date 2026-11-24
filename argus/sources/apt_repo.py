"""Debian/Ubuntu APT repository Packages parser."""

from __future__ import annotations

import gzip
import logging
import lzma
from collections.abc import Iterator

import httpx

from argus.config import AppConfig, AptRepoOptions
from argus.core.plugins import register_source
from argus.core.types import ReleaseInfo
from argus.sources.base import SourceError, SourceProvider

logger = logging.getLogger(__name__)


def _decompress(data: bytes) -> bytes:
    if data.startswith(b"\x1f\x8b"):
        return gzip.decompress(data)
    if data.startswith(b"\xfd7zXZ\x00"):
        return lzma.decompress(data)
    return data


def _parse_stanzas(text: str) -> Iterator[dict[str, str]]:
    stanza: dict[str, str] = {}
    last_key: str | None = None
    for line in text.splitlines():
        if not line.strip():
            if stanza:
                yield stanza
                stanza = {}
            last_key = None
            continue
        if line[:1] in (" ", "\t") and last_key:
            stanza[last_key] = f"{stanza[last_key]}\n{line.strip()}".strip()
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        stanza[key] = value.strip()
        last_key = key
    if stanza:
        yield stanza


def _compact(values: dict[str, str | None]) -> dict[str, str]:
    return {key: value for key, value in values.items() if value}


@register_source("apt_repo")
class AptRepoSource(SourceProvider):
    """Read the newest version of a package from an APT ``Packages`` index."""

    name = "apt_repo"
    options_model = AptRepoOptions

    def __init__(self, options: AptRepoOptions, settings: AppConfig) -> None:
        super().__init__(options, settings)
        self.options: AptRepoOptions = options
        self.default_timeout = settings.sources.apt_repo.timeout_seconds

    def _candidate_urls(self) -> list[str]:
        if self.options.packages_url:
            return [self.options.packages_url]
        base = self.options.base_url.rstrip("/")
        index = (
            f"{base}/dists/{self.options.suite}/{self.options.component}"
            f"/binary-{self.options.architecture}/Packages"
        )
        return [f"{index}.gz", f"{index}.xz", index]

    async def _download_index(self, client: httpx.AsyncClient) -> tuple[str, str]:
        errors: list[str] = []
        for url in self._candidate_urls():
            try:
                response = await self.request(client, "GET", url)
            except httpx.HTTPError as error:
                errors.append(f"{url}: {error}")
                continue
            if response.status_code >= 400:
                errors.append(f"{url}: HTTP {response.status_code}")
                continue
            try:
                raw = _decompress(response.content)
                return url, raw.decode("utf-8", errors="replace")
            except (OSError, EOFError, lzma.LZMAError) as error:
                errors.append(f"{url}: cannot decompress ({error})")
        raise SourceError(
            "unable to download APT Packages index: " + "; ".join(errors)
        )

    async def list_releases(self) -> list[ReleaseInfo]:
        async with self.create_client() as client:
            index_url, text = await self._download_index(client)

        releases: dict[str, ReleaseInfo] = {}
        for stanza in _parse_stanzas(text):
            if stanza.get("Package") != self.options.package:
                continue
            version = stanza.get("Version", "").strip()
            if not version:
                continue
            releases.setdefault(
                version,
                ReleaseInfo(
                    version=version,
                    notes=stanza.get("Description"),
                    digest=stanza.get("SHA256"),
                    metadata=_compact(
                        {
                            "package": stanza.get("Package"),
                            "version": version,
                            "architecture": stanza.get("Architecture"),
                            "section": stanza.get("Section"),
                            "priority": stanza.get("Priority"),
                            "source": stanza.get("Source"),
                            "filename": stanza.get("Filename"),
                            "size": stanza.get("Size"),
                            "sha256": stanza.get("SHA256"),
                        }
                    ),
                ),
            )
        if not releases:
            logger.debug("package %s not found in %s", self.options.package, index_url)
        return list(releases.values())

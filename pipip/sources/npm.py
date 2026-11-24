"""Package release metadata from an npm registry."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from semver import Version

from pipip.config import AppConfig, NpmOptions
from pipip.core.plugins import register_source
from pipip.core.types import ReleaseInfo
from pipip.core.utils import parse_iso8601
from pipip.core.versioning import select_latest
from pipip.sources.base import SourceError, SourceProvider


def _release_metadata(item: dict[str, Any]) -> dict[str, Any]:
    engines = item.get("engines")
    engines = engines if isinstance(engines, dict) else {}
    repository = item.get("repository")
    if isinstance(repository, dict):
        repository = repository.get("url")

    metadata: dict[str, Any] = {
        key: value.strip()
        for key, value in {
            "requires_node": engines.get("node"),
            "requires_npm": engines.get("npm"),
            "license": item.get("license"),
            "description": item.get("description"),
            "repository": repository,
            "deprecated": item.get("deprecated"),
        }.items()
        if isinstance(value, str) and value.strip()
    }
    for field, key in (
        ("dependencies", "dependencies"),
        ("optionalDependencies", "optional_dependencies"),
        ("peerDependencies", "peer_dependencies"),
        ("devDependencies", "dev_dependencies"),
    ):
        dependencies = item.get(field)
        if isinstance(dependencies, dict):
            versions = {
                name: specifier for name, specifier in dependencies.items()
                if isinstance(name, str) and isinstance(specifier, str)
            }
            if versions:
                metadata[key] = versions

    dist = item.get("dist")
    size = dist.get("unpackedSize") if isinstance(dist, dict) else None
    if type(size) is int and size >= 0:
        metadata["unpacked_size_bytes"] = size
    return metadata


@register_source("npm")
class NpmSource(SourceProvider):
    name = "npm"
    options_model = NpmOptions

    def __init__(self, options: NpmOptions, settings: AppConfig) -> None:
        super().__init__(options, settings)
        self.options: NpmOptions = options

    async def list_releases(self) -> list[ReleaseInfo]:
        package = self.options.package
        base_url = self.options.base_url.rstrip("/")
        url = f"{base_url}/{quote(package, safe='@')}"
        public_registry = base_url == "https://registry.npmjs.org"
        payload = await self.request_json(url)
        versions = payload.get("versions")
        if not isinstance(versions, dict):
            raise SourceError("unexpected npm response: versions must be an object")

        if self.options.dist_tag is not None:
            tags = payload.get("dist-tags")
            tagged = tags.get(self.options.dist_tag) if isinstance(tags, dict) else None
            if not isinstance(tagged, str) or tagged not in versions:
                raise SourceError(f"npm dist-tag {self.options.dist_tag!r} not found for {package}")
            versions = {tagged: versions[tagged]}

        times = payload.get("time")
        times = times if isinstance(times, dict) else {}
        releases: list[ReleaseInfo] = []
        for version, item in versions.items():
            if not isinstance(item, dict):
                continue
            try:
                parsed = Version.parse(version)
            except ValueError:
                continue
            date = times.get(version)
            dist = item.get("dist")
            integrity = dist.get("integrity") if isinstance(dist, dict) else None
            releases.append(
                ReleaseInfo(
                    version=version,
                    url=(
                        f"https://www.npmjs.com/package/{package}/v/{version}"
                        if public_registry else None
                    ),
                    published_at=parse_iso8601(date) if isinstance(date, str) else None,
                    prerelease=parsed.prerelease is not None,
                    digest=integrity if isinstance(integrity, str) else None,
                    metadata=_release_metadata(item),
                )
            )
        return releases

    async def latest(self) -> ReleaseInfo | None:
        return select_latest(await self.list_releases(), self.options, sort_key=Version.parse)

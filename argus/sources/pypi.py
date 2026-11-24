"""Python package releases from the PyPI Simple JSON API."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import Version

from argus.config import AppConfig, PyPIOptions
from argus.core.plugins import register_source
from argus.core.types import ReleaseInfo
from argus.core.utils import parse_iso8601
from argus.core.versioning import select_latest
from argus.sources.base import SourceError, SourceProvider


def _artifact_metadata(
    item: dict[str, Any], filename: str, *, is_wheel: bool
) -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "filename": filename,
        "type": "wheel" if is_wheel else "source archive",
    }
    if type(size := item.get("size")) is int and size >= 0:
        artifact["size_bytes"] = size
    hashes = item.get("hashes")
    if isinstance(hashes, dict):
        digests = {
            name: digest for name, digest in hashes.items()
            if isinstance(name, str) and isinstance(digest, str)
        }
        if digests:
            artifact["hashes"] = digests
    yanked = item.get("yanked", False)
    if yanked is True or isinstance(yanked, str):
        artifact["yanked"] = yanked or True
    if isinstance(required := item.get("requires-python"), str) and required.strip():
        artifact["requires_python"] = required.strip()
    if isinstance(provenance := item.get("provenance"), str) and provenance:
        artifact["provenance"] = provenance
    return artifact


@register_source("pypi")
class PyPISource(SourceProvider):
    name = "pypi"
    options_model = PyPIOptions

    def __init__(self, options: PyPIOptions, settings: AppConfig) -> None:
        super().__init__(options, settings)
        self.options: PyPIOptions = options

    async def list_releases(self) -> list[ReleaseInfo]:
        project = canonicalize_name(self.options.package)
        base_url = self.options.base_url.rstrip("/")
        url = f"{base_url}/{project}/"
        public_registry = base_url == "https://pypi.org/simple"
        payload = await self.request_json(url, accept="application/vnd.pypi.simple.v1+json")
        files = payload.get("files")
        if not isinstance(files, list):
            raise SourceError("unexpected PyPI response: files must be a list")

        published: dict[Version, datetime | None] = {}
        requirements: defaultdict[Version, set[str]] = defaultdict(set)
        artifacts: defaultdict[Version, list[dict[str, Any]]] = defaultdict(list)
        for item in files:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename")
            if not isinstance(filename, str):
                continue
            is_wheel = filename.endswith(".whl")
            try:
                if is_wheel:
                    _, version, _, _ = parse_wheel_filename(filename)
                else:
                    _, version = parse_sdist_filename(filename)
            except (InvalidWheelFilename, InvalidSdistFilename):
                continue
            yanked = item.get("yanked", False)
            if not isinstance(yanked, (bool, str)):
                continue
            artifact = _artifact_metadata(item, filename, is_wheel=is_wheel)
            artifacts[version].append(artifact)
            if yanked is not False:
                continue
            upload_time = item.get("upload-time")
            date = parse_iso8601(upload_time) if isinstance(upload_time, str) else None
            first = published.get(version)
            if first is None or (date is not None and date < first):
                published[version] = date
            if requires_python := artifact.get("requires_python"):
                requirements[version].add(requires_python)

        releases: list[ReleaseInfo] = []
        for version, date in published.items():
            specifiers = sorted(requirements.get(version, ()))
            release = ReleaseInfo(
                version=str(version),
                url=f"https://pypi.org/project/{project}/{version}/" if public_registry else None,
                published_at=date,
                prerelease=version.is_prerelease,
            )
            version_files = sorted(artifacts[version], key=lambda item: item["filename"])
            available = [item for item in version_files if "yanked" not in item]
            wheel_count = sum(item["type"] == "wheel" for item in available)
            release.metadata.update(
                wheels=wheel_count,
                source_archives=len(available) - wheel_count,
                files=version_files,
            )
            if specifiers:
                release.metadata["requires_python"] = (
                    specifiers[0] if len(specifiers) == 1 else specifiers
                )
            releases.append(release)
        return releases

    async def latest(self) -> ReleaseInfo | None:
        return select_latest(await self.list_releases(), self.options, sort_key=Version)

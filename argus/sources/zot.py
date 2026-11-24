"""Zot registry support using the shared Docker Registry v2 client."""

from __future__ import annotations

from argus.config import AppConfig, ZotOptions
from argus.core.plugins import register_source
from argus.sources.docker import DockerSource


@register_source("zot")
class ZotSource(DockerSource):
    """Fetch tags from a ZOT registry repository."""

    name = "zot"
    options_model = ZotOptions

    def __init__(self, options: ZotOptions, settings: AppConfig) -> None:
        super().__init__(options, settings)
        self.default_timeout = settings.sources.zot.timeout_seconds

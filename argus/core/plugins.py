"""Dynamic plugin registry used by source and notifier layers."""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from argus.notifiers.base import Notifier
    from argus.sources.base import SourceProvider

logger = logging.getLogger(__name__)


class PluginError(LookupError):
    """Raised when a plugin cannot be found or loaded."""


class PluginRegistry[T]:
    """Small, dependency-free registry of plugin classes."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._plugins: dict[str, type[T]] = {}

    def register(self, name: str, plugin: type[T]) -> None:
        normalized = name.strip().lower()
        if not normalized:
            raise PluginError(f"empty {self.kind} plugin name")
        existing = self._plugins.get(normalized)
        if existing is not None and existing is not plugin:
            raise PluginError(
                f"{self.kind} plugin '{normalized}' is already registered by "
                f"{existing.__module__}.{existing.__qualname__}"
            )
        self._plugins[normalized] = plugin

    def decorator(self, name: str) -> Callable[[type[T]], type[T]]:
        def register(plugin: type[T]) -> type[T]:
            self.register(name, plugin)
            return plugin

        return register

    def get(self, name: str) -> type[T]:
        try:
            return self._plugins[name.strip().lower()]
        except KeyError as error:
            available = ", ".join(self.names()) or "<none>"
            raise PluginError(
                f"unknown {self.kind} plugin '{name}' (available: {available})"
            ) from error

    def names(self) -> list[str]:
        return sorted(self._plugins)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.strip().lower() in self._plugins


SOURCES: PluginRegistry[SourceProvider] = PluginRegistry("source")
NOTIFIERS: PluginRegistry[Notifier] = PluginRegistry("notifier")


def register_source(name: str) -> Callable[[type[SourceProvider]], type[SourceProvider]]:
    return SOURCES.decorator(name)


def register_notifier(name: str) -> Callable[[type[Notifier]], type[Notifier]]:
    return NOTIFIERS.decorator(name)


def _load_entry_points(group: str, registry: PluginRegistry[Any]) -> None:
    for entry_point in importlib.metadata.entry_points(group=group):
        try:
            plugin = entry_point.load()
            name = getattr(plugin, "name", entry_point.name)
            registry.register(name, plugin)
            logger.debug("loaded %s plugin %s from entry point", registry.kind, name)
        except Exception:
            logger.exception(
                "failed to load %s plugin from entry point %s",
                registry.kind,
                entry_point,
            )


_loaded = False


def load_plugins() -> None:
    """Import built-in plugins and discover third-party entry points."""

    global _loaded
    if _loaded:
        return
    # Importing the plugin modules runs their registration decorators.
    for module in (
        "argus.notifiers.ntfy",
        "argus.sources.apt_repo",
        "argus.sources.codeberg",
        "argus.sources.debian_release",
        "argus.sources.docker",
        "argus.sources.git_tag",
        "argus.sources.github",
        "argus.sources.npm",
        "argus.sources.pypi",
        "argus.sources.zot",
    ):
        importlib.import_module(module)

    _load_entry_points("argus.sources", SOURCES)
    _load_entry_points("argus.notifiers", NOTIFIERS)
    _loaded = True

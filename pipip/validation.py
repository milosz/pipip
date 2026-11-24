"""Configuration/plugin validation used at startup."""

from __future__ import annotations

from pipip.config import AppConfig
from pipip.core.plugins import NOTIFIERS, SOURCES, PluginError, load_plugins


def validate_settings(settings: AppConfig) -> list[str]:
    """Return a list of human-readable validation errors (empty means valid)."""

    load_plugins()
    errors: list[str] = []

    for target in settings.targets:
        try:
            plugin = SOURCES.get(target.source)
        except PluginError as error:
            errors.append(f"target {target.name}: {error}")
            continue
        if not isinstance(target.options, plugin.options_model):
            errors.append(
                f"target {target.name}: options do not match "
                f"{target.source} plugin schema"
            )
            continue
        try:
            plugin(options=target.options, settings=settings)
        except Exception as error:
            errors.append(f"target {target.name}: cannot initialize source: {error}")

        for notifier in target.notifiers:
            if notifier not in NOTIFIERS:
                errors.append(
                    f"target {target.name}: unknown notifier '{notifier}' "
                    f"(available: {', '.join(NOTIFIERS.names())})"
                )

    if not settings.targets:
        errors.append("no targets are configured")

    return errors


def validate_warnings(settings: AppConfig) -> list[str]:
    """Return non-fatal security/configuration warnings."""

    warnings: list[str] = []
    if settings.security.allow_private_networks:
        warnings.append(
            "security.allow_private_networks is true; sources/notifiers may "
            "reach private or metadata addresses"
        )

    url_keys = {"api_url", "base_url", "packages_url", "url", "web_url"}
    for target in settings.targets:
        options = target.options.model_dump(mode="json")
        for key in url_keys:
            value = options.get(key)
            if isinstance(value, str) and value.startswith("http://"):
                warnings.append(
                    f"target {target.name}: insecure http:// URL in {key}"
                )
        if options.get("scheme") == "http":
            warnings.append(f"target {target.name}: registry uses http://")

    if settings.notifiers.ntfy.server.startswith("http://"):
        warnings.append("notifiers.ntfy: insecure http:// URL")
    return warnings

from __future__ import annotations

from collections.abc import Callable

import pytest

from pipip.config import AppConfig, parse_config
from pipip.core.plugins import SOURCES, PluginError
from pipip.validation import validate_settings, validate_warnings


def _docker_settings() -> AppConfig:
    return parse_config(
        {
            "targets": [
                {
                    "name": "nginx",
                    "source": "docker",
                    "options": {"image": "nginx"},
                    "notifiers": ["ntfy", "unavailable"],
                }
            ],
        }
    )


def test_validate_reports_unknown_source(monkeypatch: pytest.MonkeyPatch) -> None:
    def lookup(name: str) -> Callable:
        raise PluginError(f"unknown source {name}")

    monkeypatch.setattr(SOURCES, "get", lookup)
    assert validate_settings(_docker_settings()) == ["target nginx: unknown source docker"]


def test_validate_reports_provider_schema_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipip.sources.github import GitHubSource

    monkeypatch.setattr(SOURCES, "get", lambda name: GitHubSource)
    assert validate_settings(_docker_settings()) == [
        "target nginx: options do not match docker plugin schema"
    ]


def test_validate_reports_init_failure_and_unknown_notifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipip.sources.docker import DockerSource

    def fail(*args, **kwargs):
        raise ValueError("invalid image configuration")

    monkeypatch.setattr(DockerSource, "__init__", fail)
    errors = validate_settings(_docker_settings())
    assert errors[0] == "target nginx: cannot initialize source: invalid image configuration"
    assert len(errors) == 2
    assert errors[1].startswith("target nginx: unknown notifier 'unavailable' (available:")


def test_validate_requires_targets() -> None:
    settings = parse_config({})
    assert validate_settings(settings) == ["no targets are configured"]


def test_validate_warns_about_insecure_endpoints() -> None:
    settings = parse_config(
        {
            "scheduler": {"enabled": True},
            "security": {"allow_private_networks": True},
            "notifiers": {
                "ntfy": {"server": "http://ntfy.test"},
            },
            "targets": [
                {
                    "name": "nginx",
                    "source": "docker",
                    "options": {"image": "nginx", "scheme": "http"},
                },
                {
                    "name": "debian",
                    "source": "debian_release",
                    "options": {"base_url": "http://apt.test"},
                },
            ],
        }
    )
    assert set(validate_warnings(settings)) == {
        "security.allow_private_networks is true; sources/notifiers may "
        "reach private or metadata addresses",
        "target nginx: registry uses http://",
        "target debian: insecure http:// URL in base_url",
        "notifiers.ntfy: insecure http:// URL",
    }

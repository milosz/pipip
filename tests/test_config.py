from __future__ import annotations

import pytest
from pydantic import ValidationError

from argus.config import load_config, parse_config


def test_environment_interpolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGUS_TEST_TOKEN", "s3cret")
    settings = parse_config(
        {
            "sources": {"github": {"token": "${ARGUS_TEST_TOKEN}"}},
            "targets": [
                {
                    "name": "example",
                    "source": "github",
                    "options": {"repo": "owner/repo"},
                }
            ],
        }
    )
    assert settings.sources.github.token is not None
    assert settings.sources.github.token.get_secret_value() == "s3cret"


def test_empty_secret_becomes_none() -> None:
    settings = parse_config(
        {"sources": {"github": {"token": "${MISSING_TOKEN}"}}}
    )
    assert settings.sources.github.token is None


def test_duplicate_targets_rejected() -> None:
    target = {
        "name": "same",
        "source": "docker",
        "options": {"image": "library/nginx"},
    }
    with pytest.raises(ValidationError):
        parse_config({"targets": [target, target]})


def test_sqlite_url_normalization() -> None:
    for url in ("sqlite:///./data/argus.db", "sqlite:///:memory:"):
        settings = parse_config({"database": {"url": url}})
        assert settings.database.url == url.replace("sqlite:", "sqlite+aiosqlite:")
    with pytest.raises(ValidationError, match="must use SQLite"):
        parse_config({"database": {"url": "postgresql://db/argus"}})


def test_zot_target_requires_registry() -> None:
    settings = parse_config(
        {
            "targets": [
                {
                    "name": "zot",
                    "source": "zot",
                    "options": {
                        "image": "library/nginx",
                        "registry": "zot.internal:5000",
                        "username": "user",
                        "password": "secret",
                    },
                }
            ],
        }
    )
    target = settings.targets[0]
    assert target.source == "zot"
    assert target.options.registry == "zot.internal:5000"
    assert target.options.password is not None
    assert target.options.password.get_secret_value() == "secret"

    with pytest.raises(ValidationError):
        parse_config(
            {
                    "targets": [
                    {
                        "name": "zot",
                        "source": "zot",
                        "options": {"image": "library/nginx"},
                    }
                ],
            }
        )

def test_database_url_environment_override(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "config.yaml"
    path.write_text(
        "targets: []\ndatabase:\n  url: sqlite+aiosqlite:///./x.db\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "ARGUS_DATABASE_URL", "sqlite+aiosqlite:///:memory:"
    )
    settings = load_config(path)
    assert (
        settings.database.url
        == "sqlite+aiosqlite:///:memory:"
    )


def test_repository_config_is_valid() -> None:
    from argus.validation import validate_settings

    assert not validate_settings(load_config("config.yaml"))

"""Pydantic v2 configuration schema for Argus.

The configuration is loaded from a YAML file. Every string value may contain
environment variable references in the form ``${NAME}`` or ``${NAME:-default}``.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised when the Argus configuration cannot be loaded or validated."""


def _expand_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` references."""

    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            default = match.group(2)
            env_value = os.environ.get(name)
            if env_value is not None:
                return env_value
            return default if default is not None else ""

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _empty_secret_to_none(cls: Any, value: Any) -> Any:
    """Treat blank environment substitutions as an unset secret."""

    if isinstance(value, SecretStr):
        return value if value.get_secret_value().strip() else None
    if isinstance(value, str):
        return value if value.strip() else None
    return value


class DatabaseSettings(_StrictModel):
    url: str = "sqlite+aiosqlite:///./data/argus.db"

    @field_validator("url")
    @classmethod
    def _sqlite_only(cls, value: str) -> str:
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError

        try:
            url = make_url(value.strip())
        except ArgumentError as error:
            raise ValueError("invalid SQLite URL") from error
        if url.drivername not in {"sqlite", "sqlite+aiosqlite", "sqlite+pysqlite"}:
            raise ValueError("database.url must use SQLite")
        if url.query or url.host or url.username or url.password or url.port:
            raise ValueError("database.url must point to a local SQLite file or :memory:")
        return "sqlite+aiosqlite:" + value.strip().split(":", 1)[1]


class HistorySettings(_StrictModel):
    retention: int = Field(default=200, ge=1, le=100000)


class RetrySettings(_StrictModel):
    attempts: int = Field(default=3, ge=1, le=10)
    backoff_seconds: float = Field(default=5.0, ge=0)
    max_backoff_seconds: float = Field(default=300.0, ge=0)
    retry_statuses: list[int] = Field(
        default_factory=lambda: [429, 500, 502, 503, 504]
    )


class SchedulerSettings(_StrictModel):
    enabled: bool = True
    timezone: str = "UTC"
    default_interval_seconds: int = Field(default=21600, ge=10)
    jitter_seconds: int = Field(default=30, ge=0)
    max_concurrency: int = Field(default=5, ge=1)
    run_on_start: bool = True
    notification_dispatch_seconds: int = Field(default=60, ge=5)
    outbox_purge_hours: int = Field(default=24, ge=1)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown timezone: {value}") from error
        return value


class HttpSettings(_StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    root_path: str = ""
    access_log: bool = False

    @field_validator("root_path")
    @classmethod
    def _normalize_root_path(cls, value: str) -> str:
        path = value.strip().strip("/")
        return f"/{path}" if path else ""


class SecuritySettings(_StrictModel):
    allow_private_networks: bool = False


class SourceSettings(_StrictModel):
    timeout_seconds: float = Field(default=30.0, gt=0)


class GitHubSourceSettings(SourceSettings):
    api_url: str = "https://api.github.com"
    token: SecretStr | None = None

    _normalize_token = field_validator("token", mode="before")(_empty_secret_to_none)


class CodebergSourceSettings(SourceSettings):
    base_url: str = "https://codeberg.org"
    token: SecretStr | None = None

    _normalize_token = field_validator("token", mode="before")(_empty_secret_to_none)


class SourcesSettings(_StrictModel):
    docker: SourceSettings = Field(default_factory=SourceSettings)
    zot: SourceSettings = Field(default_factory=SourceSettings)
    github: GitHubSourceSettings = Field(default_factory=GitHubSourceSettings)
    codeberg: CodebergSourceSettings = Field(default_factory=CodebergSourceSettings)
    git_tag: SourceSettings = Field(default_factory=SourceSettings)
    apt_repo: SourceSettings = Field(default_factory=SourceSettings)
    debian_release: SourceSettings = Field(default_factory=SourceSettings)


class NtfySettings(_StrictModel):
    enabled: bool = False
    server: str = ""
    topic: str = ""
    token: SecretStr | None = None
    username: str | None = None
    password: SecretStr | None = None
    priority: int = Field(default=3, ge=1, le=5)
    tags: list[str] = Field(default_factory=lambda: ["package"])
    timeout_seconds: float = Field(default=15.0, gt=0)

    _normalize_secrets = field_validator("token", "password", mode="before")(
        _empty_secret_to_none
    )

    @field_validator("priority", mode="before")
    @classmethod
    def _normalize_priority(cls, value: Any) -> Any:
        if isinstance(value, str):
            priorities = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}
            return priorities.get(value.strip().lower(), value)
        return value

    @model_validator(mode="after")
    def _require_destination(self) -> NtfySettings:
        if self.enabled and not (self.server.strip() and self.topic.strip()):
            raise ValueError("notifiers.ntfy requires both server and topic when enabled")
        return self


class NotifierSettings(_StrictModel):
    notify_on_initial: bool = False
    max_attempts: int = Field(default=5, ge=1, le=100)
    retry_backoff_seconds: float = Field(default=60.0, ge=0)
    outbox_retention_days: int = Field(default=30, ge=1, le=3650)
    ntfy: NtfySettings = Field(default_factory=NtfySettings)


class DashboardSettings(_StrictModel):
    refresh_seconds: int = Field(default=30, ge=5)


class RecentSettings(_StrictModel):
    days: int = Field(default=84, ge=7, le=730)
    limit: int = Field(default=8, ge=1, le=500)


class BadgesSettings(_StrictModel):
    enabled: bool = False
    targets: list[str] | Literal["all"] = "all"


class ApiSettings(_StrictModel):
    enabled: bool = False


class MetricsSettings(_StrictModel):
    enabled: bool = False


class AtomSettings(_StrictModel):
    enabled: bool = False
    targets: list[str] | Literal["all"] = "all"
    limit: int = Field(default=50, ge=1, le=500)


class SourceOptions(_StrictModel):
    """Options shared by every source provider."""

    include_prerelease: bool = False
    version_regex: str | None = Field(default=None, max_length=200)
    max_candidates: int = Field(default=2000, ge=1, le=100000)
    timeout_seconds: float | None = Field(default=None, gt=0)

    @field_validator("version_regex")
    @classmethod
    def _validate_regex(cls, value: str | None) -> str | None:
        if value:
            try:
                re.compile(value)
            except re.error as error:  # pragma: no cover - defensive
                raise ValueError(f"invalid version_regex: {error}") from error
        return value


class DockerOptions(SourceOptions):
    image: str = Field(min_length=1)
    registry: str | None = None
    scheme: Literal["https", "http"] = "https"
    username: str | None = None
    password: SecretStr | None = None
    token: SecretStr | None = None
    max_pages: int = Field(default=20, ge=1, le=100)

    _normalize_secrets = field_validator("password", "token", mode="before")(
        _empty_secret_to_none
    )


class ZotOptions(DockerOptions):
    """Options for a ZOT/OCI registry that requires an explicit registry host."""

    registry: str = Field(min_length=1)


class GitHubOptions(SourceOptions):
    repo: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    token: SecretStr | None = None
    use_releases: bool = True
    use_tags: bool = True
    max_pages: int = Field(default=3, ge=1, le=20)

    _normalize_token = field_validator("token", mode="before")(_empty_secret_to_none)


class CodebergOptions(SourceOptions):
    repo: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    base_url: str | None = None
    token: SecretStr | None = None
    use_releases: bool = True
    use_tags: bool = True
    max_pages: int = Field(default=3, ge=1, le=20)

    _normalize_token = field_validator("token", mode="before")(_empty_secret_to_none)


class GitTagOptions(SourceOptions):
    url: str = Field(min_length=1)
    web_url: str | None = None


class AptRepoOptions(SourceOptions):
    package: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    suite: str = Field(min_length=1)
    component: str = "main"
    architecture: str = "amd64"
    packages_url: str | None = None


class DebianReleaseOptions(SourceOptions):
    base_url: str = Field(default="https://deb.debian.org/debian", min_length=1)
    suite: str = Field(default="stable", min_length=1)


class PyPIOptions(SourceOptions):
    package: str = Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
    base_url: str = Field(default="https://pypi.org/simple", min_length=1)


class NpmOptions(SourceOptions):
    package: str = Field(pattern=r"^(?:@[a-z0-9_][a-z0-9._-]*/)?[a-z0-9_][a-z0-9._-]*$")
    base_url: str = Field(default="https://registry.npmjs.org", min_length=1)
    dist_tag: str | None = Field(default=None, min_length=1)


class _TargetBase(_StrictModel):
    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    enabled: bool = True
    group: str | None = None
    hide_in_dashboard: bool = False
    interval_seconds: int | None = Field(default=None, ge=10)
    notifiers: list[str] = Field(default_factory=list)


class DockerTarget(_TargetBase):
    source: Literal["docker"]
    options: DockerOptions


class ZotTarget(_TargetBase):
    source: Literal["zot"]
    options: ZotOptions


class GitHubTarget(_TargetBase):
    source: Literal["github"]
    options: GitHubOptions


class CodebergTarget(_TargetBase):
    source: Literal["codeberg"]
    options: CodebergOptions


class GitTagTarget(_TargetBase):
    source: Literal["git_tag"]
    options: GitTagOptions


class AptRepoTarget(_TargetBase):
    source: Literal["apt_repo"]
    options: AptRepoOptions


class DebianReleaseTarget(_TargetBase):
    source: Literal["debian_release"]
    options: DebianReleaseOptions


class PyPITarget(_TargetBase):
    source: Literal["pypi"]
    options: PyPIOptions


class NpmTarget(_TargetBase):
    source: Literal["npm"]
    options: NpmOptions


type TargetConfig = Annotated[
    DockerTarget
    | ZotTarget
    | GitHubTarget
    | CodebergTarget
    | GitTagTarget
    | AptRepoTarget
    | DebianReleaseTarget
    | PyPITarget
    | NpmTarget,
    Field(discriminator="source"),
]


class ComparisonConfig(_StrictModel):
    """Compare one or more monitored targets against a reference."""

    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    reference: str = Field(min_length=1)
    targets: list[str] = Field(min_length=1)
    mode: Literal["exact", "semver"] = "exact"
    enabled: bool = True
    description: str | None = None


class AppConfig(_StrictModel):
    name: str = "Argus"
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    history: HistorySettings = Field(default_factory=HistorySettings)
    recent: RecentSettings = Field(default_factory=RecentSettings)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    sources: SourcesSettings = Field(default_factory=SourcesSettings)
    notifiers: NotifierSettings = Field(default_factory=NotifierSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    atom: AtomSettings = Field(default_factory=AtomSettings)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)
    badges: BadgesSettings = Field(default_factory=BadgesSettings)
    targets: list[TargetConfig] = Field(default_factory=list)
    comparisons: list[ComparisonConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_targets_and_comparisons(self) -> AppConfig:
        target_names = Counter(target.name for target in self.targets)
        duplicates = sorted(name for name, count in target_names.items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate target names: {', '.join(duplicates)}")

        for comparison in self.comparisons:
            sides = [comparison.reference, *comparison.targets]
            if len(set(sides)) != len(sides):
                raise ValueError(
                    f"comparison {comparison.name!r} requires distinct reference and targets"
                )
            for side in sides:
                if side not in target_names:
                    raise ValueError(
                        f"comparison {comparison.name!r} references unknown "
                        f"target {side!r}"
                    )
        comparison_names = Counter(comparison.name for comparison in self.comparisons)
        duplicates = sorted(name for name, count in comparison_names.items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate comparison names: {', '.join(duplicates)}")

        return self


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate an Argus YAML configuration file."""

    config_path = Path(
        path or os.environ.get("ARGUS_CONFIG", "config.yaml")
    ).expanduser()
    if not config_path.exists():
        raise ConfigError(f"configuration file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ConfigError(f"invalid YAML in {config_path}: {error}") from error
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"configuration root must be a mapping in {config_path}")
    database_url = os.environ.get("ARGUS_DATABASE_URL")
    if database_url:
        database = raw.setdefault("database", {})
        if isinstance(database, dict):
            database["url"] = database_url
    try:
        return parse_config(raw)
    except Exception as error:
        raise ConfigError(f"invalid configuration in {config_path}: {error}") from error


def parse_config(data: dict[str, Any]) -> AppConfig:
    """Validate an in-memory configuration mapping (used by tests and tooling)."""

    return AppConfig.model_validate(_expand_env(data))

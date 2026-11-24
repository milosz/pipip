"""Core orchestration engine that checks targets and stores results."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from argus.config import AppConfig, TargetConfig
from argus.core.plugins import SOURCES, PluginError, load_plugins
from argus.core.types import CheckResult, Notification
from argus.core.utils import safe_log, truncate
from argus.db.models import (
    Target,
    VersionDigestHistory,
    VersionRecord,
    utcnow,
)
from argus.db.session import Database
from argus.notifiers.outbox import Outbox
from argus.sources.base import SourceProvider

logger = logging.getLogger(__name__)
_STORED_RELEASE_NOTES_LIMIT = 10_000


class TargetNotFound(LookupError):
    """Raised when a requested target/application does not exist."""


class ArgusEngine:
    """Coordinates sources, persistence and notifications."""

    def __init__(self, settings: AppConfig, database: Database) -> None:
        load_plugins()
        self.settings = settings
        self.database = database
        self.targets: dict[str, TargetConfig] = {
            target.name: target for target in settings.targets
        }
        self._semaphore = asyncio.Semaphore(settings.scheduler.max_concurrency)
        self._locks: dict[str, asyncio.Lock] = {}
        self.outbox = Outbox(settings, database)

    async def sync_targets(self) -> int:
        """Create, update or prune database rows from the configured targets."""

        configured = {target.name: target for target in self.settings.targets}
        async with self.database.session() as session:
            existing = {
                app.name: app
                for app in (await session.scalars(select(Target))).all()
            }
            for name, stale_app in existing.items():
                if name not in configured:
                    await self._purge_app(session, stale_app)
                    logger.info("removed target %r: no longer in configuration", name)

            for target in configured.values():
                payload = target.model_dump(mode="json")
                app = existing.get(target.name)
                if app is None:
                    app = Target(
                        name=target.name,
                        source=target.source,
                        target_config=payload,
                        enabled=target.enabled,
                        status="pending",
                    )
                    session.add(app)
                else:
                    app.source = target.source
                    app.target_config = payload
                    app.enabled = target.enabled
                    if app.status == "removed":  # Archived by an older release.
                        app.status = "pending"
        self.targets = configured
        count = len(configured)
        logger.debug("synchronized %d targets", count)
        return count

    async def _purge_app(self, session: AsyncSession, app: Target) -> None:
        await session.execute(
            delete(VersionDigestHistory).where(
                VersionDigestHistory.application_id == app.id
            )
        )
        await session.execute(
            delete(VersionRecord).where(VersionRecord.application_id == app.id)
        )
        await session.execute(
            delete(Target).where(Target.id == app.id)
        )

    def _make_source(self, target: TargetConfig) -> SourceProvider:
        try:
            plugin = SOURCES.get(target.source)
        except PluginError as error:
            raise TargetNotFound(str(error)) from error
        return plugin(options=target.options, settings=self.settings)

    async def _mark_error(self, name: str, error: str) -> CheckResult:
        now = utcnow()
        message = truncate(error, 2000) or "unknown error"
        async with self.database.session() as session:
            app = await session.scalar(select(Target).where(Target.name == name))
            if app is None:
                raise TargetNotFound(f"target {name!r} is not tracked")
            app.status = "error"
            app.last_error = message
            app.last_checked_at = now
            source = app.source
        logger.warning("check failed for %s: %s", name, safe_log(message))
        return CheckResult(
            target=name,
            source=source,
            status="error",
            error=message,
            checked_at=now,
        )

    async def _prune_history(self, session: AsyncSession, application_id: int) -> None:
        await session.flush()
        keep_ids = (
            select(VersionRecord.id)
            .where(VersionRecord.application_id == application_id)
            .order_by(
                VersionRecord.is_current.desc(),
                VersionRecord.detected_at.desc(),
                VersionRecord.id.desc(),
            )
            .limit(self.settings.history.retention)
        )
        await session.execute(
            delete(VersionDigestHistory).where(
                VersionDigestHistory.application_id == application_id,
                VersionDigestHistory.version_record_id.not_in(keep_ids),
            )
        )
        await session.execute(
            delete(VersionRecord).where(
                VersionRecord.application_id == application_id,
                VersionRecord.id.not_in(keep_ids),
            )
        )

    async def _check_application(self, target: TargetConfig) -> CheckResult:
        name = target.name
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            async with self.database.session() as session:
                app = await session.scalar(
                    select(Target).where(Target.name == name)
                )
                if app is None:
                    raise TargetNotFound(f"target {name!r} is not tracked")
                old_version = app.current_version
                source_name = app.source

            try:
                source = self._make_source(target)
                async with self._semaphore:
                    release = await source.latest()
            except Exception as error:
                return await self._mark_error(name, str(error))

            now = utcnow()
            if release is None:
                async with self.database.session() as session:
                    app = await session.scalar(
                        select(Target).where(Target.name == name)
                    )
                    if app is None:
                        raise TargetNotFound(f"target {name!r} is not tracked")
                    app.status = "no_release"
                    app.last_error = "no releases matched the configured filters"
                    app.last_checked_at = now
                logger.info("no matching release found for %s", name)
                return CheckResult(
                    target=name,
                    source=source_name,
                    status="no_release",
                    previous_version=old_version,
                    error="no releases matched the configured filters",
                    checked_at=now,
                )

            new_version = release.version
            version_changed = old_version != new_version
            digest_changed = False
            previous_digest: str | None = None
            async with self.database.session() as session:
                app = await session.scalar(
                    select(Target).where(Target.name == name)
                )
                if app is None:
                    raise TargetNotFound(f"target {name!r} is not tracked")
                app.status = "ok"
                app.last_error = None
                app.last_checked_at = now
                if version_changed:
                    app.previous_version = old_version
                    app.current_version = new_version
                    app.last_changed_at = now
                    await session.execute(
                        update(VersionRecord)
                        .where(
                            VersionRecord.application_id == app.id,
                            VersionRecord.is_current.is_(True),
                        )
                        .values(is_current=False)
                    )
                    new_record = VersionRecord(
                        application_id=app.id,
                        version=new_version,
                        release_url=release.url,
                        release_notes=truncate(release.notes, _STORED_RELEASE_NOTES_LIMIT),
                        digest=release.digest,
                        metadata_json=release.metadata,
                        published_at=release.published_at,
                        detected_at=now,
                        is_current=True,
                    )
                    session.add(new_record)
                    await session.flush()
                    if release.digest:
                        session.add(
                            VersionDigestHistory(
                                application_id=app.id,
                                version_record_id=new_record.id,
                                version=new_version,
                                previous_digest=None,
                                digest=release.digest,
                                detected_at=now,
                            )
                        )
                else:
                    record = await session.scalar(
                        select(VersionRecord).where(
                            VersionRecord.application_id == app.id,
                            VersionRecord.is_current.is_(True),
                        )
                    )
                    if record is not None:
                        previous_digest = record.digest
                        if (
                            record.digest
                            and release.digest
                            and record.digest != release.digest
                        ):
                            digest_changed = True
                        if release.digest and record.digest != release.digest:
                            session.add(
                                VersionDigestHistory(
                                    application_id=app.id,
                                    version_record_id=record.id,
                                    version=new_version,
                                    previous_digest=record.digest,
                                    digest=release.digest,
                                    detected_at=now,
                                )
                            )
                        if release.digest:
                            record.digest = release.digest
                        if release.metadata:
                            record.metadata_json = release.metadata
                        record.release_url = release.url
                        if release.published_at:
                            record.published_at = release.published_at
                        if release.notes:
                            record.release_notes = truncate(
                                release.notes, _STORED_RELEASE_NOTES_LIMIT
                            )

                await self._prune_history(session, app.id)

                if version_changed and (
                    old_version is not None
                    or self.settings.notifiers.notify_on_initial
                ):
                    notification = Notification(
                        app_name=name,
                        source=source_name,
                        old_version=old_version,
                        new_version=new_version,
                        release_url=release.url,
                        release_notes=release.notes,
                        idempotency_key=f"version:{new_record.id}:{now.isoformat()}",
                        event_type="version",
                    )
                    await self.outbox.enqueue(session, target, notification)
                elif digest_changed:
                    old_short = (previous_digest or "?")[:12]
                    new_short = (release.digest or "?")[:12]
                    notification = Notification(
                        app_name=name,
                        source=source_name,
                        old_version=old_version,
                        new_version=new_version,
                        release_url=release.url,
                        release_notes=release.notes,
                        title_override=f"{name}: digest changed",
                        message_override=f"Version {new_version} · {old_short} → {new_short}",
                        idempotency_key=(
                            f"digest:{name}:{new_version}:"
                            f"{previous_digest or ''}:{release.digest or ''}"
                        ),
                        event_type="digest",
                    )
                    await self.outbox.enqueue(session, target, notification)

            if version_changed:
                logger.info(
                    "new version for %s: %s -> %s",
                    name,
                    safe_log(old_version or "<none>"),
                    safe_log(new_version),
                )
            elif digest_changed:
                logger.info(
                    "digest changed for %s at %s: %s -> %s",
                    name,
                    safe_log(new_version),
                    safe_log(previous_digest or "<none>"),
                    safe_log(release.digest or "<none>"),
                )
            else:
                logger.debug(
                    "version for %s unchanged (%s)", name, safe_log(new_version)
                )
            return CheckResult(
                target=name,
                source=source_name,
                status="ok",
                previous_version=old_version,
                new_version=new_version,
                changed=version_changed or digest_changed,
                digest_changed=digest_changed,
                checked_at=now,
            )

    async def run_checks(self, target: str | None = None) -> list[CheckResult]:
        """Check synchronized targets, either one by name or all enabled targets."""
        if target is None:
            candidates = sorted(self.targets.values(), key=lambda item: item.name)
        else:
            config = self.targets.get(target)
            if config is None:
                raise TargetNotFound(f"target {target!r} is not tracked")
            candidates = [config]
        enabled = [config for config in candidates if config.enabled]
        if not enabled:
            return []

        raw_results = await asyncio.gather(
            *(self._check_application(config) for config in enabled), return_exceptions=True
        )
        results: list[CheckResult] = []
        for config, result in zip(enabled, raw_results, strict=True):
            name = config.name
            if isinstance(result, BaseException):
                if isinstance(result, TargetNotFound):
                    if target is not None:
                        raise result
                    logger.error("target disappeared during check: %s", name)
                    continue
                results.append(
                    CheckResult(
                        target=name,
                        source="unknown",
                        status="error",
                        error=str(result),
                        checked_at=utcnow(),
                    )
                )
            else:
                results.append(result)
        await self.outbox.dispatch()
        return results

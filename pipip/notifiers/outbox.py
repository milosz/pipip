"""Durable delivery, deduplication and retries for notifications."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import asdict
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pipip.config import AppConfig, TargetConfig
from pipip.core.plugins import NOTIFIERS, PluginError, load_plugins
from pipip.core.types import Notification
from pipip.core.utils import safe_log, truncate
from pipip.db.models import NotificationOutbox, utcnow
from pipip.db.session import Database
from pipip.notifiers.base import Notifier

logger = logging.getLogger(__name__)


class Outbox:
    def __init__(self, settings: AppConfig, database: Database) -> None:
        load_plugins()
        self.settings = settings
        self.database = database
        self._notifier_cache: dict[str, Notifier | None] = {}
        self._dispatch_lock = asyncio.Lock()

    def _notifier_names(self, target: TargetConfig) -> list[str]:
        if target.notifiers:
            return list(dict.fromkeys(target.notifiers))
        return ["ntfy"] if self.settings.notifiers.ntfy.enabled else []

    def _get_notifier(self, name: str) -> Notifier | None:
        if name in self._notifier_cache:
            return self._notifier_cache[name]
        try:
            plugin = NOTIFIERS.get(name)
        except PluginError:
            logger.error("unknown notifier plugin %r", name)
            self._notifier_cache[name] = None
            return None
        section = getattr(self.settings.notifiers, name, None)
        if section is not None and getattr(section, "enabled", True) is False:
            logger.debug("notifier %s is disabled in configuration", name)
            self._notifier_cache[name] = None
            return None
        notifier = plugin(settings=self.settings)
        self._notifier_cache[name] = notifier
        return notifier

    def _outbox_key(self, notification: Notification, notifier: str) -> str:
        base = notification.idempotency_key or (
            f"{notification.event_type}:{notification.app_name}:"
            f"{notification.source}:{notification.old_version}:"
            f"{notification.new_version}"
        )
        return hashlib.sha256(f"{base}:{notifier}".encode()).hexdigest()

    async def enqueue(
        self,
        session: AsyncSession,
        target: TargetConfig,
        notification: Notification,
    ) -> int:
        """Persist outbox entries inside the caller's transaction."""

        enqueued = 0
        for name in self._notifier_names(target):
            if self._get_notifier(name) is None:
                continue
            statement = insert(NotificationOutbox).values(
                idempotency_key=self._outbox_key(notification, name),
                notifier=name,
                app_name=notification.app_name,
                event_type=notification.event_type,
                payload=asdict(notification),
            ).on_conflict_do_nothing(index_elements=[NotificationOutbox.idempotency_key])
            result = await session.execute(statement)
            enqueued += int(getattr(result, "rowcount", 0))
        return enqueued

    def _apply_failure(
        self, row: NotificationOutbox, error: Exception
    ) -> None:
        row.attempts += 1
        row.last_error = truncate(str(error), 2000)
        if row.attempts >= self.settings.notifiers.max_attempts:
            row.status = "dead"
            row.next_attempt_at = utcnow()
            return
        row.status = "failed"
        delay = self.settings.notifiers.retry_backoff_seconds * (
            2 ** (row.attempts - 1)
        )
        delay = min(delay, self.settings.retry.max_backoff_seconds)
        if delay > 0:
            delay *= random.uniform(0.75, 1.0)
        row.next_attempt_at = utcnow() + timedelta(seconds=delay)

    async def purge(self) -> int:
        """Delete sent and dead entries older than the configured retention."""
        cutoff = utcnow() - timedelta(days=self.settings.notifiers.outbox_retention_days)
        async with self.database.session() as session:
            result = await session.execute(
                delete(NotificationOutbox).where(
                    NotificationOutbox.status.in_(("sent", "dead")),
                    NotificationOutbox.created_at < cutoff,
                )
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def _deliver(self, row: NotificationOutbox) -> None:
        notifier = self._get_notifier(row.notifier)
        failure: Exception | None = None
        if notifier is not None:
            notification = Notification(**row.payload)
            notification.idempotency_key = row.idempotency_key
            try:
                await notifier.send(notification)
            except Exception as error:
                failure = error

        async with self.database.session() as session:
            fresh = await session.get(NotificationOutbox, row.id)
            if fresh is not None:
                if notifier is None:
                    fresh.status = "dead"
                    fresh.last_error = "notifier is not configured or enabled"
                elif failure is not None:
                    self._apply_failure(fresh, failure)
                else:
                    sent_at = utcnow()
                    fresh.status = "sent"
                    fresh.sent_at = sent_at
                    fresh.attempts += 1
                    fresh.last_error = None
                    fresh.next_attempt_at = sent_at
        if failure is not None:
            logger.warning(
                "notification retry failed for %s: %s", row.notifier, safe_log(failure)
            )

    async def dispatch(self, limit: int = 100) -> int:
        """Retry due outbox entries and return the number processed."""
        async with self._dispatch_lock:
            due = (
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.status.in_(("pending", "failed")),
                    NotificationOutbox.next_attempt_at <= utcnow(),
                )
                .order_by(NotificationOutbox.next_attempt_at, NotificationOutbox.id)
                .limit(limit)
            )
            async with self.database.session() as session:
                rows = list(await session.scalars(due))
            for row in rows:
                await self._deliver(row)
            return len(rows)

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import timedelta

import pytest
from sqlalchemy import select

from pipip.config import AppConfig, parse_config
from pipip.core.types import Notification
from pipip.db.models import NotificationOutbox, utcnow
from pipip.db.session import Database
from pipip.notifiers.outbox import Outbox


def make_settings() -> AppConfig:
    return parse_config({
        "database": {"url": "sqlite:///:memory:"},
        "targets": [{
            "name": "example", "source": "docker", "options": {"image": "nginx"},
            "notifiers": ["ntfy"],
        }],
    })


@pytest.mark.asyncio
async def test_notification_outbox_deduplicates_same_event() -> None:
    settings = make_settings()
    database = Database(settings.database)
    await database.init()
    try:
        outbox = Outbox(settings, database)
        sent: list[Notification] = []

        class FakeNotifier:
            async def send(self, notification: Notification) -> None:
                sent.append(notification)

        outbox._get_notifier = lambda name: FakeNotifier()  # type: ignore[method-assign]
        notification = Notification(
            app_name="example",
            source="docker",
            old_version="1.0.0",
            new_version="1.1.0",
            idempotency_key="version:example:1.1.0",
            event_type="version",
        )

        target = settings.targets[0]
        async with database.session() as session:
            assert await outbox.enqueue(session, target, notification) == 1
            assert await outbox.enqueue(session, target, notification) == 0

        await outbox.dispatch()

        assert len(sent) == 1
        async with database.session() as session:
            rows = list(
                (await session.scalars(select(NotificationOutbox))).all()
            )
            assert len(rows) == 1
            assert rows[0].status == "sent"
            assert rows[0].payload == asdict(notification)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_outbox_retries_a_failed_delivery() -> None:
    settings = make_settings()
    settings.notifiers.retry_backoff_seconds = 0
    database = Database(settings.database)
    await database.init()
    try:
        outbox = Outbox(settings, database)
        delivered: list[Notification] = []

        class FlakyNotifier:
            attempts = 0

            async def send(self, notification: Notification) -> None:
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("temporarily unavailable")
                delivered.append(notification)

        notifier = FlakyNotifier()
        outbox._get_notifier = lambda name: notifier  # type: ignore[method-assign]
        notification = Notification(
            app_name="example", source="docker", old_version="1.0.0", new_version="1.1.0",
        )
        async with database.session() as session:
            await outbox.enqueue(session, settings.targets[0], notification)

        assert await outbox.dispatch() == 1
        assert not delivered
        async with database.session() as session:
            row = await session.scalar(select(NotificationOutbox))
            assert row is not None
            assert row.status == "failed"
            assert row.attempts == 1
            assert row.last_error == "temporarily unavailable"

        assert await outbox.dispatch() == 1
        assert len(delivered) == 1
        async with database.session() as session:
            row = await session.scalar(select(NotificationOutbox))
            assert row is not None
            assert row.status == "sent"
            assert row.attempts == 2
            assert row.last_error is None
            assert row.sent_at is not None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_outbox_enqueue_rolls_back_with_caller() -> None:
    settings = make_settings()
    settings.notifiers.ntfy.enabled = True
    database = Database(settings.database)
    await database.init()
    try:
        outbox = Outbox(settings, database)
        notification = Notification(
            app_name="example", source="docker", old_version="1.0.0", new_version="1.1.0",
        )
        with pytest.raises(RuntimeError, match="rollback"):
            async with database.session() as session:
                assert await outbox.enqueue(session, settings.targets[0], notification) == 1
                raise RuntimeError("rollback")
        async with database.session() as session:
            assert await session.scalar(select(NotificationOutbox)) is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_purge_notifications_respects_retention_and_status() -> None:
    settings = make_settings()
    settings.notifiers.outbox_retention_days = 30
    database = Database(settings.database)
    await database.init()
    try:
        outbox = Outbox(settings, database)
        now = utcnow()
        async with database.session() as session:
            for key, status, age in (
                ("old-sent", "sent", 40),
                ("old-dead", "dead", 40),
                ("old-pending", "pending", 40),
                ("new-sent", "sent", 1),
            ):
                session.add(
                    NotificationOutbox(
                        idempotency_key=key,
                        notifier="ntfy",
                        app_name="example",
                        event_type="version",
                        payload={},
                        status=status,
                        attempts=1,
                        next_attempt_at=now,
                        created_at=now - timedelta(days=age),
                    )
                )

        purged = await outbox.purge()

        assert purged == 2
        async with database.session() as session:
            remaining = {
                row.idempotency_key
                for row in (
                    await session.scalars(select(NotificationOutbox))
                ).all()
            }
            assert remaining == {"old-pending", "new-sent"}
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_dispatch_notifications_is_serialized() -> None:
    settings = make_settings()
    database = Database(settings.database)
    await database.init()
    try:
        outbox = Outbox(settings, database)
        sent: list[Notification] = []

        class SlowNotifier:
            async def send(self, notification: Notification) -> None:
                sent.append(notification)
                await asyncio.sleep(0.01)

        outbox._get_notifier = lambda name: SlowNotifier()  # type: ignore[method-assign]
        notification = Notification(
            app_name="example",
            source="docker",
            old_version="1.0.0",
            new_version="1.1.0",
            idempotency_key="serialized-dispatch",
        )
        async with database.session() as session:
            session.add(
                NotificationOutbox(
                    idempotency_key="serialized-dispatch",
                    notifier="ntfy",
                    app_name="example",
                    event_type="version",
                    payload=asdict(notification),
                    status="pending",
                    attempts=0,
                    next_attempt_at=utcnow(),
                )
            )

        results = await asyncio.gather(
            outbox.dispatch(),
            outbox.dispatch(),
        )

        assert results == [1, 0]
        assert len(sent) == 1
    finally:
        await database.dispose()

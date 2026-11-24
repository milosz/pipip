from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from argus.config import (
    AppConfig,
    DatabaseSettings,
    DockerOptions,
    DockerTarget,
    HistorySettings,
)
from argus.core.engine import ArgusEngine, TargetNotFound
from argus.core.types import Notification, ReleaseInfo
from argus.db.models import (
    NotificationOutbox,
    Target,
    VersionDigestHistory,
    VersionRecord,
)
from argus.db.session import Database


def make_settings(*, retention: int = 200) -> AppConfig:
    return AppConfig(
        database=DatabaseSettings(url="sqlite+aiosqlite:///:memory:"),
        history=HistorySettings(retention=retention),
        targets=[
            DockerTarget(
                name="example",
                source="docker",
                options=DockerOptions(
                    image="library/nginx",
                    version_regex=r"^\d+\.\d+\.\d+$",
                ),
            )
        ],
    )


@pytest.mark.asyncio
async def test_engine_records_version_history(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings()
    settings.targets.append(settings.targets[0].model_copy(
        update={"name": "disabled", "enabled": False}
    ))
    database = Database(settings.database)
    await database.init()
    try:
        engine = ArgusEngine(settings, database)
        await engine.sync_targets()

        async def unexpected_sync() -> int:
            pytest.fail("checks must not synchronize targets after startup")

        monkeypatch.setattr(engine, "sync_targets", unexpected_sync)
        versions = iter(
            [
                ReleaseInfo(
                    "1.0.0",
                    url="https://example.test/release/1.0.0",
                    digest="a" * 64,
                    metadata={"version": "1.0.0"},
                ),
                ReleaseInfo(
                    "1.1.0",
                    url="https://example.test/release/1.1.0",
                    digest="b" * 64,
                    metadata={"version": "1.1.0", "artist": "one"},
                ),
                ReleaseInfo(
                    "1.1.0",
                    url="https://example.test/release/1.1.0",
                    digest="b" * 64,
                    metadata={"version": "1.1.0", "artist": "two"},
                ),
            ]
        )

        class FakeSource:
            async def latest(self) -> ReleaseInfo:
                return next(versions)

        engine._make_source = lambda target: FakeSource()  # type: ignore[method-assign]

        assert await engine.run_checks("disabled") == []
        with pytest.raises(TargetNotFound, match="missing"):
            await engine.run_checks("missing")

        first = await engine.run_checks()
        assert [result.target for result in first] == ["example"]
        assert first[0].status == "ok"
        assert first[0].changed is True
        assert first[0].new_version == "1.0.0"

        second = await engine.run_checks()
        assert second[0].changed is True
        assert second[0].previous_version == "1.0.0"
        assert second[0].new_version == "1.1.0"

        third = await engine.run_checks()
        assert third[0].changed is False
        assert third[0].new_version == "1.1.0"

        async with database.session() as session:
            app = await session.scalar(
                select(Target).where(Target.name == "example")
            )
            assert app is not None
            assert app.current_version == "1.1.0"
            assert app.previous_version == "1.0.0"
            assert app.status == "ok"
            count = await session.scalar(
                select(func.count()).select_from(VersionRecord)
            )
            assert count == 2
            current = await session.scalar(
                select(VersionRecord).where(VersionRecord.is_current.is_(True))
            )
            assert current is not None
            assert current.digest == "b" * 64
            assert current.metadata_json == {"version": "1.1.0", "artist": "two"}
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_engine_records_a_return_to_an_earlier_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    settings.notifiers.notify_on_initial = True
    settings.targets[0].notifiers = ["ntfy"]
    database = Database(settings.database)
    await database.init()
    try:
        engine = ArgusEngine(settings, database)
        await engine.sync_targets()
        versions = iter(("1.0.0", "2.0.0", "1.0.0", "1.0.0"))
        sent: list[tuple[str | None, str]] = []

        class FakeSource:
            async def latest(self) -> ReleaseInfo:
                version = next(versions)
                return ReleaseInfo(version, digest=version)

        class FakeNotifier:
            async def send(self, notification: Notification) -> None:
                sent.append((notification.old_version, notification.new_version))

        monkeypatch.setattr(engine, "_make_source", lambda target: FakeSource())
        monkeypatch.setattr(engine.outbox, "_get_notifier", lambda name: FakeNotifier())

        for changed in (True, True, True, False):
            (result,) = await engine.run_checks("example")
            assert result.status == "ok"
            assert result.changed is changed

        target = await database.get_target("example")
        assert target is not None
        assert (target.previous_version, target.current_version) == ("2.0.0", "1.0.0")
        history = await database.version_history(target.id, limit=10)
        assert [(record.version, record.is_current) for record in reversed(history)] == [
            ("1.0.0", False),
            ("2.0.0", False),
            ("1.0.0", True),
        ]
        expected = [(None, "1.0.0"), ("1.0.0", "2.0.0"), ("2.0.0", "1.0.0")]
        assert sent == expected
        recent = await database.recent_changes(days=7, limit=10)
        assert [(entry.previous_version, entry.version) for entry in reversed(recent)] == expected
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_engine_marks_errors() -> None:
    settings = make_settings()
    database = Database(settings.database)
    await database.init()
    try:
        engine = ArgusEngine(settings, database)
        await engine.sync_targets()

        class BrokenSource:
            async def latest(self) -> ReleaseInfo:
                raise RuntimeError("boom")

        engine._make_source = lambda target: BrokenSource()  # type: ignore[method-assign]
        results = await engine.run_checks()
        assert results[0].status == "error"
        assert "boom" in (results[0].error or "")

        async with database.session() as session:
            app = await session.scalar(
                select(Target).where(Target.name == "example")
            )
            assert app is not None
            assert app.status == "error"
            assert app.last_error is not None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_sync_targets_resets_legacy_removed_status() -> None:
    settings = make_settings()
    database = Database(settings.database)
    await database.init()
    try:
        engine = ArgusEngine(settings, database)
        await engine.sync_targets()
        async with database.session() as session:
            session.add(
                Target(
                    name="gone",
                    source="docker",
                    target_config={},
                    status="removed",
                    enabled=False,
                )
            )

        settings.targets.append(
            DockerTarget(
                name="gone",
                source="docker",
                options=DockerOptions(image="library/busybox"),
            )
        )
        await engine.sync_targets()

        async with database.session() as session:
            app = await session.scalar(
                select(Target).where(Target.name == "gone")
            )
            assert app is not None
            assert app.status == "pending"
            assert app.enabled is True
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_sync_targets_deletes_removed_target_history() -> None:
    settings = make_settings()
    database = Database(settings.database)
    await database.init()
    try:
        engine = ArgusEngine(settings, database)
        await engine.sync_targets()
        async with database.session() as session:
            gone = Target(
                name="gone",
                source="docker",
                target_config={},
                status="removed",
                enabled=False,
            )
            session.add(gone)
            await session.flush()
            record = VersionRecord(
                application_id=gone.id,
                version="1.0.0",
                digest="a" * 64,
                is_current=True,
            )
            session.add(record)
            await session.flush()
            session.add(
                VersionDigestHistory(
                    application_id=gone.id,
                    version_record_id=record.id,
                    version="1.0.0",
                    digest="a" * 64,
                )
            )

        await engine.sync_targets()

        async with database.session() as session:
            assert (
                await session.scalar(
                    select(Target).where(Target.name == "gone")
                )
                is None
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(VersionRecord)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(VersionDigestHistory)
                )
                == 0
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_engine_prunes_history_beyond_retention() -> None:
    settings = make_settings(retention=2)
    database = Database(settings.database)
    await database.init()
    try:
        engine = ArgusEngine(settings, database)
        await engine.sync_targets()
        versions = iter(["1.0.0", "1.1.0", "1.2.0"])

        class FakeSource:
            async def latest(self) -> ReleaseInfo:
                return ReleaseInfo(next(versions))

        engine._make_source = lambda target: FakeSource()  # type: ignore[method-assign]

        for _ in range(3):
            await engine.run_checks()

        async with database.session() as session:
            records = list(
                (
                    await session.scalars(
                        select(VersionRecord).order_by(VersionRecord.id)
                    )
                ).all()
            )
            assert [record.version for record in records] == ["1.1.0", "1.2.0"]
            assert sum(1 for record in records if record.is_current) == 1
            assert records[-1].version == "1.2.0"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_engine_notifies_on_digest_change() -> None:
    settings = make_settings()
    database = Database(settings.database)
    await database.init()
    try:
        engine = ArgusEngine(settings, database)
        await engine.sync_targets()
        settings.targets[0].notifiers = ["ntfy"]
        digests = iter(["a" * 64, "b" * 64])

        class FakeSource:
            async def latest(self) -> ReleaseInfo:
                return ReleaseInfo("1.0.0", digest=next(digests))

        engine._make_source = lambda target: FakeSource()  # type: ignore[method-assign]
        sent: list[object] = []

        class FakeNotifier:
            async def send(self, notification: object) -> None:
                sent.append(notification)

        engine.outbox._get_notifier = lambda name: FakeNotifier()  # type: ignore[method-assign]

        first = await engine.run_checks()
        assert first[0].changed is True
        assert first[0].digest_changed is False

        second = await engine.run_checks()
        assert second[0].changed is True
        assert second[0].digest_changed is True
        assert len(sent) == 1
        assert "digest changed" in sent[0].title  # type: ignore[attr-defined]

        async with database.session() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(VersionRecord)
                )
                == 1
            )
            record = await session.scalar(
                select(VersionRecord).where(VersionRecord.is_current.is_(True))
            )
            assert record is not None
            assert record.digest == "b" * 64
            digest_rows = list(
                (
                    await session.scalars(
                        select(VersionDigestHistory).order_by(
                            VersionDigestHistory.id
                        )
                    )
                ).all()
            )
            assert len(digest_rows) == 2
            assert digest_rows[0].previous_digest is None
            assert digest_rows[0].digest == "a" * 64
            assert digest_rows[1].previous_digest == "a" * 64
            assert digest_rows[1].digest == "b" * 64
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_prune_keeps_other_apps_digest_history() -> None:
    settings = make_settings(retention=1)
    settings.targets.append(
        DockerTarget(
            name="other",
            source="docker",
            options=DockerOptions(image="library/busybox"),
        )
    )
    database = Database(settings.database)
    await database.init()
    try:
        engine = ArgusEngine(settings, database)
        await engine.sync_targets()
        calls = {"example": 0}

        class FakeSource:
            def __init__(self, name: str) -> None:
                self.name = name

            async def latest(self) -> ReleaseInfo:
                if self.name == "other":
                    return ReleaseInfo("9.9.9", digest="c" * 64)
                calls["example"] += 1
                version = "1.0.0" if calls["example"] == 1 else "1.1.0"
                digest = "a" * 64 if calls["example"] == 1 else "b" * 64
                return ReleaseInfo(version, digest=digest)

        engine._make_source = lambda target: FakeSource(target.name)  # type: ignore[method-assign]

        await engine.run_checks()
        await engine.run_checks("example")

        async with database.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(VersionDigestHistory).order_by(
                            VersionDigestHistory.application_id,
                            VersionDigestHistory.id,
                        )
                    )
                ).all()
            )
            versions = {row.version for row in rows}
            assert versions == {"1.1.0", "9.9.9"}
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_pending_outbox_survives_engine_restart(tmp_path: Path) -> None:
    settings = make_settings()
    settings.targets[0].notifiers = ["ntfy"]
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path / 'argus.db'}"
    database = Database(settings.database)
    await database.init()
    try:
        versions = iter(["1.0.0", "1.1.0"])

        class FakeSource:
            async def latest(self) -> ReleaseInfo:
                return ReleaseInfo(next(versions), digest="a" * 64)

        class FakeNotifier:
            def __init__(self) -> None:
                self.sent: list[Notification] = []

            async def send(self, notification: Notification) -> None:
                self.sent.append(notification)

        first_engine = ArgusEngine(settings, database)
        await first_engine.sync_targets()
        first_engine._make_source = lambda target: FakeSource()  # type: ignore[method-assign]
        first_engine.outbox._get_notifier = lambda name: FakeNotifier()  # type: ignore[method-assign]

        async def no_dispatch(limit: int = 100) -> int:
            return 0

        first_engine.outbox.dispatch = no_dispatch  # type: ignore[method-assign]
        await first_engine.run_checks()
        await first_engine.run_checks()

        async with database.session() as session:
            pending = list(
                (
                    await session.scalars(
                        select(NotificationOutbox).where(
                            NotificationOutbox.status == "pending"
                        )
                    )
                ).all()
            )
            assert len(pending) == 1

        await database.dispose()
        database = Database(settings.database)
        await database.init()
        notifier = FakeNotifier()
        second_engine = ArgusEngine(settings, database)
        second_engine.outbox._get_notifier = lambda name: notifier  # type: ignore[method-assign]
        processed = await second_engine.outbox.dispatch()

        assert processed == 1
        assert len(notifier.sent) == 1
        async with database.session() as session:
            row = await session.scalar(select(NotificationOutbox))
            assert row is not None
            assert row.status == "sent"
    finally:
        await database.dispose()

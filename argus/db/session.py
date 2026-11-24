"""SQLite database and transaction management."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select, text, tuple_
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import ScalarSelect

from argus.config import DatabaseSettings
from argus.core.types import RecentChange
from argus.db.models import Base, Target, VersionRecord


def previous_detected_version() -> ScalarSelect[str]:
    """Find the previous version in retained history for a VersionRecord query."""
    earlier = aliased(VersionRecord)
    return (
        select(earlier.version)
        .where(
            earlier.application_id == VersionRecord.application_id,
            tuple_(earlier.detected_at, earlier.id)
            < tuple_(VersionRecord.detected_at, VersionRecord.id),
        )
        .order_by(earlier.detected_at.desc(), earlier.id.desc())
        .limit(1)
        .correlate(VersionRecord)
        .scalar_subquery()
    )


class Database:
    def __init__(self, settings: DatabaseSettings) -> None:
        url = make_url(settings.url)
        database = url.database
        self.path: Path | None = None
        if database and database != ":memory:":
            self.path = Path(database).expanduser().resolve()
            url = url.set(database=str(self.path))
        self.engine = create_async_engine(url)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        # In-memory SQLite uses one connection. Keep transactions from overlapping.
        self._transaction_lock = asyncio.Lock()

    async def init(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._transaction_lock, self._sessions.begin() as session:
            yield session

    async def list_targets(self) -> list[Target]:
        async with self.session() as session:
            return list(await session.scalars(select(Target).order_by(Target.name)))

    async def get_target(self, name: str) -> Target | None:
        async with self.session() as session:
            return await session.scalar(select(Target).where(Target.name == name))

    async def version_history(self, application_id: int, limit: int) -> list[VersionRecord]:
        async with self.session() as session:
            return list(await session.scalars(
                select(VersionRecord)
                .where(VersionRecord.application_id == application_id)
                .order_by(VersionRecord.detected_at.desc(), VersionRecord.id.desc())
                .limit(limit)
            ))

    async def recent_changes(self, *, days: int, limit: int) -> list[RecentChange]:
        today = datetime.now(UTC).date()
        start = datetime.combine(today - timedelta(days=days - 1), time.min, tzinfo=UTC)
        query = (
            select(
                Target.name, VersionRecord.version, VersionRecord.detected_at,
                previous_detected_version(),
            )
            .join(VersionRecord, VersionRecord.application_id == Target.id)
            .where(VersionRecord.detected_at >= start)
            .order_by(VersionRecord.detected_at.desc(), VersionRecord.id.desc())
            .limit(limit)
        )
        async with self.session() as session:
            rows = (await session.execute(query)).all()
        return [
            RecentChange(
                target=name,
                version=version,
                previous_version=previous,
                detected_at=(
                    detected_at.astimezone(UTC)
                    if detected_at.tzinfo else detected_at.replace(tzinfo=UTC)
                ),
            )
            for name, version, detected_at, previous in rows
        ]

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def ping(self) -> bool:
        try:
            async with self.session() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

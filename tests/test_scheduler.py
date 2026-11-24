from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from argus.config import (
    AppConfig,
    DockerOptions,
    DockerTarget,
    SchedulerSettings,
)
from argus.core.engine import ArgusEngine
from argus.db.session import Database
from argus.scheduler import ArgusScheduler


def _settings(*, run_on_start: bool) -> AppConfig:
    return AppConfig(
        scheduler=SchedulerSettings(
            run_on_start=run_on_start,
            jitter_seconds=0,
        ),
        targets=[
            DockerTarget(
                name="example",
                source="docker",
                options=DockerOptions(image="library/nginx"),
            )
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("run_on_start", "delay_seconds"), [(True, 5), (False, 21600)])
async def test_run_on_start_controls_first_check(
    run_on_start: bool, delay_seconds: int
) -> None:
    settings = _settings(run_on_start=run_on_start)
    database = Database(settings.database)
    scheduler = ArgusScheduler(settings, ArgusEngine(settings, database))
    try:
        before = datetime.now(UTC)
        await scheduler.start()
        after = datetime.now(UTC)
        assert scheduler._scheduler is not None
        job = scheduler._scheduler.get_job("check:example")
        assert job is not None
        assert job.next_run_time is not None
        delay = timedelta(seconds=delay_seconds)
        assert before + delay <= job.next_run_time <= after + delay
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_disabled_scheduler_still_runs_notification_jobs() -> None:
    settings = _settings(run_on_start=True)
    settings.scheduler.enabled = False
    database = Database(settings.database)
    scheduler = ArgusScheduler(settings, ArgusEngine(settings, database))
    try:
        await scheduler.start()
        assert scheduler._scheduler is not None
        job_ids = {job.id for job in scheduler._scheduler.get_jobs()}
        assert "check:example" not in job_ids
        assert "notifications:dispatch" in job_ids
        assert "notifications:purge" in job_ids
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_shutdown_waits_for_active_check_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(run_on_start=False)
    settings.database.url = "sqlite+aiosqlite:///:memory:"
    database = Database(settings.database)
    await database.init()
    engine = ArgusEngine(settings, database)
    started = asyncio.Event()
    cleaning_up = asyncio.Event()
    allow_cleanup = asyncio.Event()
    finished = asyncio.Event()

    async def check(target: str) -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning_up.set()
            await allow_cleanup.wait()
            assert await database.ping()
            finished.set()

    monkeypatch.setattr(engine, "run_checks", check)
    scheduler = ArgusScheduler(settings, engine)
    try:
        await scheduler.start()
        assert scheduler._scheduler is not None
        scheduler._scheduler.modify_job("check:example", next_run_time=datetime.now(UTC))
        await asyncio.wait_for(started.wait(), timeout=2)
        shutdown = asyncio.create_task(scheduler.shutdown())
        try:
            await asyncio.wait_for(cleaning_up.wait(), timeout=2)
            assert not shutdown.done()
        finally:
            allow_cleanup.set()
            await asyncio.wait_for(shutdown, timeout=2)
        assert finished.is_set()
        assert not scheduler.running
    finally:
        allow_cleanup.set()
        await scheduler.shutdown()
        await database.dispose()

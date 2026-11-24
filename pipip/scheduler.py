"""APScheduler integration for periodic background checks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from pipip.config import AppConfig
from pipip.core.engine import PipipEngine

logger = logging.getLogger(__name__)


class PipipScheduler:
    """Manage one interval job per configured target."""

    def __init__(self, settings: AppConfig, engine: PipipEngine) -> None:
        self.settings = settings
        self.engine = engine
        self._scheduler: AsyncIOScheduler | None = None
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._shutdown_lock = asyncio.Lock()
        self._timezone = ZoneInfo(settings.scheduler.timezone)

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    def _track_job[**P, T](self, job: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        async def run(*args: P.args, **kwargs: P.kwargs) -> T:
            task = asyncio.current_task()
            assert task is not None
            self._active_tasks.add(task)
            try:
                return await job(*args, **kwargs)
            finally:
                self._active_tasks.discard(task)

        return run

    async def start(self) -> None:
        if self._scheduler is not None:
            return
        config = self.settings.scheduler
        scheduler = AsyncIOScheduler(
            timezone=self._timezone,
            job_defaults={"max_instances": 1, "coalesce": True},
        )
        first_run = (
            datetime.now(self._timezone) + timedelta(seconds=5)
            if config.run_on_start
            else None
        )
        if config.enabled:
            check = self._track_job(self.engine.run_checks)
            for target in self.settings.targets:
                if not target.enabled:
                    continue
                interval = (
                    target.interval_seconds
                    or config.default_interval_seconds
                )
                job_kwargs: dict[str, Any] = {
                    "trigger": "interval",
                    "seconds": interval,
                    "args": [target.name],
                    "id": f"check:{target.name}",
                    "name": f"Check {target.name}",
                    "replace_existing": True,
                    "jitter": config.jitter_seconds,
                    "misfire_grace_time": max(30, interval),
                }
                if first_run is not None:
                    job_kwargs["next_run_time"] = first_run
                scheduler.add_job(check, **job_kwargs)
                logger.debug("scheduled %s every %ss", target.name, interval)
        else:
            logger.info("target checks are disabled; notifications jobs only")
        scheduler.add_job(
            self._track_job(self.engine.outbox.purge),
            trigger="interval",
            hours=config.outbox_purge_hours,
            id="notifications:purge",
            name="Purge notification outbox",
            replace_existing=True,
            next_run_time=datetime.now(self._timezone) + timedelta(hours=1),
        )
        scheduler.add_job(
            self._track_job(self.engine.outbox.dispatch),
            trigger="interval",
            seconds=config.notification_dispatch_seconds,
            id="notifications:dispatch",
            name="Dispatch notifications",
            replace_existing=True,
            next_run_time=datetime.now(self._timezone) + timedelta(seconds=10),
        )
        scheduler.start()
        self._scheduler = scheduler
        logger.info("scheduler started with %d jobs", len(scheduler.get_jobs()))

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            scheduler = self._scheduler
            if scheduler is None:
                return
            stopped = asyncio.Event()
            scheduler.add_listener(lambda _: stopped.set(), EVENT_SCHEDULER_SHUTDOWN)
            scheduler.pause()
            scheduler.shutdown(wait=False)
            # APScheduler queues shutdown on the loop and cancels its async jobs.
            await stopped.wait()
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
            self._scheduler = None
            logger.info("scheduler stopped")

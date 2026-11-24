"""Server-rendered HTML dashboard output module."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from argus import __version__
from argus.config import AppConfig
from argus.core.comparisons import build_comparison_results
from argus.core.types import RecentChange
from argus.db.models import (
    Target,
    VersionDigestHistory,
)
from argus.db.session import Database
from argus.outputs.presentation import format_timestamp, status_label

_UPDATED_WINDOWS: dict[str, tuple[timedelta, str]] = {
    "8h": (timedelta(hours=8), "Last 8 hours"),
    "24h": (timedelta(hours=24), "Last 24 hours"),
    "3d": (timedelta(days=3), "Last 3 days"),
    "7d": (timedelta(days=7), "Last 7 days"),
}
_RELEASE_LINK_LABELS = {
    "npm": "View on npm",
    "pypi": "View on PyPI",
    "docker": "View tag on Docker Hub",
    "zot": "View tag on Docker Hub",
    "debian_release": "View release file",
    "git_tag": "View Git tag",
}
UpdatedWindow = Annotated[
    str | None,
    Query(pattern=r"^(?:" + "|".join(map(re.escape, _UPDATED_WINDOWS)) + r")$"),
]


def _changed_since(app: Target, cutoff: datetime) -> bool:
    changed_at = app.last_changed_at
    if changed_at is None:
        return False
    if changed_at.tzinfo is None:
        changed_at = changed_at.replace(tzinfo=UTC)
    return changed_at >= cutoff


def _release_link(source: str, url: str | None) -> tuple[str, str] | None:
    if not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    # OCI registries provide an API, but many have no web page for tags.
    if source in {"docker", "zot"} and parsed.hostname != "hub.docker.com":
        return None
    return url, _RELEASE_LINK_LABELS.get(source, "View release")


class Dashboard:
    def __init__(self, settings: AppConfig, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.targets = {target.name: target for target in settings.targets}
        self.config = settings.dashboard
        template_dir = Path(__file__).parent / "templates"
        self.templates = Jinja2Templates(directory=str(template_dir))
        self.templates.env.globals["fmt_dt"] = format_timestamp
        self.templates.env.globals["status_label"] = status_label
        self.templates.env.globals["app_version"] = __version__

    async def _recent_changes(self) -> dict[date, list[RecentChange]]:
        changes = await self.database.recent_changes(
            days=self.settings.recent.days, limit=self.settings.recent.limit
        )
        by_day: dict[date, list[RecentChange]] = {}
        for change in changes:
            by_day.setdefault(change.detected_at.date(), []).append(change)
        return by_day

    async def _digest_history(
        self, application_id: int
    ) -> list[VersionDigestHistory]:
        async with self.database.session() as session:
            return list(
                (
                    await session.scalars(
                        select(VersionDigestHistory)
                        .where(
                            VersionDigestHistory.application_id == application_id
                        )
                        .order_by(
                            VersionDigestHistory.detected_at.desc(),
                            VersionDigestHistory.id.desc(),
                        )
                        .limit(self.settings.history.retention)
                    )
                ).all()
            )

    def _visible_apps(self, apps: list[Target]) -> list[Target]:
        hidden = {target.name for target in self.targets.values() if target.hide_in_dashboard}
        return [
            app
            for app in apps
            if app.name not in hidden
        ]

    def _attention_reason(self, app: Target, now: datetime) -> str | None:
        if not app.enabled:
            return None
        if app.status == "error":
            return "Check failed"
        if app.status == "no_release" or not app.current_version:
            return "No detected version"

        scheduler = self.settings.scheduler
        target = self.targets.get(app.name)
        if not scheduler.enabled or target is None:
            return None
        interval = target.interval_seconds or scheduler.default_interval_seconds
        jitter = scheduler.jitter_seconds

        last_check = app.last_checked_at
        if last_check is None:
            return "Check overdue"
        if last_check.tzinfo is None:
            last_check = last_check.replace(tzinfo=UTC)
        if now - last_check > timedelta(seconds=interval + jitter):
            return "Check overdue"
        return None

    def _base_context(self, request: Request) -> dict[str, object]:
        return {
            "request": request,
            "title": self.settings.name,
            "refresh_seconds": self.config.refresh_seconds,
            "dashboard_url": request.url_for("dashboard").path,
        }

    def _context(
        self,
        request: Request,
        apps: list[Target],
        *,
        attention: bool = False,
        updated: str | None = None,
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        attention_reasons = {
            app.name: reason
            for app in apps
            if (reason := self._attention_reason(app, now)) is not None
        }
        if attention:
            updated = None
            visible_rows = [app for app in apps if app.name in attention_reasons]
        elif updated is not None:
            duration, _ = _UPDATED_WINDOWS[updated]
            cutoff = now - duration
            visible_rows = [app for app in apps if _changed_since(app, cutoff)]
        else:
            visible_rows = apps

        app_groups: dict[str, list[Target]] = {}
        for app in visible_rows:
            target = self.targets.get(app.name)
            group = (target.group if target else None) or "Ungrouped"
            app_groups.setdefault(group, []).append(app)
        app_groups = dict(sorted(
            app_groups.items(), key=lambda item: (item[0] == "Ungrouped", item[0].casefold())
        ))
        context = self._base_context(request)
        context.update(
            {
                "apps": visible_rows,
                "attention": attention,
                "updated": updated,
                "updated_windows": _UPDATED_WINDOWS,
                "attention_reasons": attention_reasons,
                "app_groups": app_groups,
                "counts": Counter(app.status for app in apps),
                "total": len(apps),
                "now": now,
            }
        )
        return context

    def router(self) -> APIRouter:
        router = APIRouter(tags=["dashboard"])

        @router.get("/", response_class=HTMLResponse)
        async def dashboard(
            request: Request, attention: bool = False, updated: UpdatedWindow = None
        ) -> HTMLResponse:
            apps = await self.database.list_targets()
            context = self._context(
                request,
                self._visible_apps(apps),
                attention=attention,
                updated=updated,
            )
            context.update(
                api_enabled=self.settings.api.enabled,
                atom_enabled=self.settings.atom.enabled,
                badges_enabled=self.settings.badges.enabled,
            )
            return self.templates.TemplateResponse(
                request=request, name="dashboard.html", context=context,
                headers={"Cache-Control": "no-store"},
            )

        @router.get("/app/{name}", response_class=HTMLResponse)
        async def app_detail(request: Request, name: str) -> HTMLResponse:
            app = await self.database.get_target(name)
            if app is None:
                raise HTTPException(status_code=404, detail=f"unknown target {name!r}")
            history = await self.database.version_history(
                app.id, self.settings.history.retention
            )
            release_url = next(
                (
                    record.release_url for record in history
                    if record.is_current and record.version == app.current_version
                ),
                None,
            )
            context = self._base_context(request)
            context.update(
                {
                    "app": app,
                    "history": history,
                    "release_link": _release_link(app.source, release_url),
                    "digest_history": await self._digest_history(app.id),
                    "now": datetime.now(UTC),
                }
            )
            return self.templates.TemplateResponse(
                request=request,
                name="detail.html",
                context=context,
                headers={"Cache-Control": "no-store"},
            )

        async def recent_changes_page(request: Request) -> HTMLResponse:
            context = self._base_context(request)
            context.update(
                recent_changes=await self._recent_changes(),
                recent_changes_limit=self.settings.recent.limit,
                recent_changes_days=self.settings.recent.days,
            )
            return self.templates.TemplateResponse(
                request=request, name="recent_changes.html", context=context,
                headers={"Cache-Control": "no-store"},
            )

        async def comparisons_page(request: Request) -> HTMLResponse:
            apps = await self.database.list_targets()
            comparisons = build_comparison_results(self.settings.comparisons, apps)
            checked_at = {
                app.name: app.last_checked_at for app in apps if app.last_checked_at is not None
            }
            latest_checks = {}
            for comparison in comparisons:
                names = [comparison.reference, *(target.name for target in comparison.targets)]
                latest_checks[comparison.name] = max(
                    (checked_at[name] for name in names if name in checked_at), default=None
                )
            context = self._base_context(request)
            context.update(
                reference_results=comparisons,
                comparison_counts=Counter(comparison.status for comparison in comparisons),
                latest_checks=latest_checks,
                atom_enabled=self.settings.atom.enabled,
            )
            return self.templates.TemplateResponse(
                request=request, name="comparisons.html", context=context,
                headers={"Cache-Control": "no-store"},
            )

        router.add_api_route("/comparisons", comparisons_page, response_class=HTMLResponse)
        router.add_api_route("/recent", recent_changes_page, response_class=HTMLResponse)
        return router

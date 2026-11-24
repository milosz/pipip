"""Embeddable version badges and their HTML catalog."""

from __future__ import annotations

from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, select

from pipip import __version__
from pipip.config import AppConfig
from pipip.core.utils import truncate
from pipip.db.models import Target
from pipip.db.session import Database
from pipip.outputs.presentation import format_timestamp, status_label

_CACHE_HEADERS = {"Cache-Control": "no-store"}
_SOURCE_LABELS = {
    "docker": "Docker",
    "zot": "Zot",
    "github": "GitHub",
    "codeberg": "Codeberg",
    "git_tag": "Git",
    "apt_repo": "APT",
    "debian_release": "Debian",
}
_SOURCE_FIELDS = {
    "docker": ("registry", "image"),
    "zot": ("registry", "image"),
    "github": ("repo",),
    "codeberg": ("repo",),
    "apt_repo": ("package", "suite"),
    "debian_release": ("suite",),
}


def _source_label(app: Target) -> str:
    return _SOURCE_LABELS.get(app.source, app.source)


def _source_detail(app: Target) -> str:
    options = (app.target_config or {}).get("options") or {}
    if app.source == "git_tag":
        try:
            url = urlsplit(options.get("url") or "")
            return f"{url.hostname}{url.path}" if url.hostname else ""
        except ValueError:
            return ""
    return " · ".join(
        value for field in _SOURCE_FIELDS.get(app.source, ())
        if isinstance(value := options.get(field), str) and value
    )


def _badge_svg(name: str, source: str, version: str | None) -> str:
    label = f"{truncate(name, 32)} · {source}"
    value = truncate(version, 32) or "pending"
    left, right = len(label) * 7 + 16, len(value) * 7 + 16
    width = left + right
    color = "#2563eb" if version else "#52525b"
    title = escape(f"{name} · {source}: {version or 'pending'}")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="22"
 viewBox="0 0 {width} 22" role="img" aria-labelledby="title">
 <title id="title">{title}</title>
 <defs><clipPath id="pill"><rect width="{width}" height="22" rx="11"/></clipPath></defs>
 <g clip-path="url(#pill)">
  <rect width="{left}" height="22" fill="#27272a"/>
  <rect x="{left}" width="{right}" height="22" fill="{color}"/>
 </g>
 <g fill="white" text-anchor="middle" font-family="monospace" font-size="11">
  <text x="{left / 2}" y="15">{escape(label)}</text>
  <text x="{left + right / 2}" y="15">{escape(value)}</text>
 </g>
</svg>"""


class BadgesOutput:
    def __init__(self, settings: AppConfig, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.config = settings.badges
        self.templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
        self.templates.env.globals.update(
            fmt_dt=format_timestamp,
            status_label=status_label,
            source_label=_source_label,
            source_detail=_source_detail,
        )

    def _targets(self) -> Select[Target]:
        query = select(Target)
        targets = self.config.targets
        if targets != "all":
            query = query.where(Target.name.in_(targets))
        return query

    def router(self) -> APIRouter:
        router = APIRouter(tags=["badges"])

        @router.get("/badges", response_class=HTMLResponse)
        async def badge_catalog(request: Request) -> HTMLResponse:
            async with self.database.session() as session:
                apps = (await session.scalars(self._targets().order_by(Target.name))).all()
            return self.templates.TemplateResponse(
                request=request,
                name="badges.html",
                context={
                    "apps": apps,
                    "title": self.settings.name,
                    "app_version": __version__,
                    "dashboard_url": request.url_for("dashboard").path,
                },
                headers=_CACHE_HEADERS,
            )

        @router.get("/badge/{target}.svg", name="version_badge")
        async def version_badge(target: str) -> Response:
            async with self.database.session() as session:
                app = await session.scalar(self._targets().where(Target.name == target))
            if app is None:
                raise HTTPException(status_code=404, detail="badge not found")
            return Response(
                _badge_svg(app.name, _source_label(app), app.current_version),
                media_type="image/svg+xml",
                headers=_CACHE_HEADERS,
            )

        return router

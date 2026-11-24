"""Read-only release API and interactive Swagger documentation."""

from __future__ import annotations

from base64 import b64encode
from datetime import datetime
from hashlib import sha256
from html import escape
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from argus import __version__
from argus.config import AppConfig
from argus.core.comparisons import (
    ReferenceComparisonResult,
    build_comparison_results,
)
from argus.core.types import RecentChange
from argus.db.models import Target, VersionRecord
from argus.db.session import Database

_MAX_HISTORY_LIMIT = 5_000


class TargetSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    source: str
    enabled: bool
    status: str
    current_version: str | None
    previous_version: str | None
    last_checked_at: datetime | None
    last_changed_at: datetime | None


class Release(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version: str
    digest: str | None
    detected_at: datetime
    is_current: bool


def api_router(settings: AppConfig, database: Database) -> APIRouter:
    router = APIRouter()

    @router.get("/api/apps", response_model=list[TargetSummary])
    async def list_targets() -> list[Target]:
        """List monitored targets and their current status."""
        return await database.list_targets()

    async def get_target_or_404(name: str) -> Target:
        target = await database.get_target(name)
        if target is None:
            raise HTTPException(status_code=404, detail="Unknown target")
        return target

    @router.get("/api/apps/{name}", response_model=TargetSummary)
    async def target_detail(name: str) -> Target:
        """Get the current state of a target."""
        return await get_target_or_404(name)

    @router.get("/api/apps/{name}/history", response_model=list[Release])
    async def target_history(
        name: str,
        limit: Annotated[int, Query(ge=1, le=_MAX_HISTORY_LIMIT)] = min(
            settings.history.retention, _MAX_HISTORY_LIMIT
        ),
    ) -> list[VersionRecord]:
        """List the most recent versions detected for a target."""
        target = await get_target_or_404(name)
        return await database.version_history(target.id, limit)

    @router.get("/api/recent")
    async def recent_changes() -> list[RecentChange]:
        """List recent version detections across all targets, newest first."""
        return await database.recent_changes(days=settings.recent.days, limit=settings.recent.limit)

    @router.get("/api/comparisons")
    async def comparisons() -> list[ReferenceComparisonResult]:
        """Compare monitored targets against their configured reference versions."""
        return build_comparison_results(
            settings.comparisons, await database.list_targets()
        )

    @router.get("/api/comparisons/{name}")
    async def comparison_detail(name: str) -> ReferenceComparisonResult:
        """Get the result of one enabled comparison by its configured name."""
        config = next(
            (item for item in settings.comparisons if item.enabled and item.name == name),
            None,
        )
        if config is None:
            raise HTTPException(status_code=404, detail="Unknown comparison")
        (result,) = build_comparison_results([config], await database.list_targets())
        return result

    schema = get_openapi(title=f"{settings.name} API", version=__version__, routes=router.routes)

    @router.get("/openapi.json", include_in_schema=False)
    async def api_schema(request: Request) -> dict[str, object]:
        root_path = request.scope.get("root_path", "").rstrip("/")
        return {**schema, "servers": [{"url": root_path or "/"}]}

    @router.get("/docs", include_in_schema=False)
    async def swagger(request: Request) -> HTMLResponse:
        response = get_swagger_ui_html(
            openapi_url=request.url_for("api_schema").path,
            title=f"{escape(settings.name)} API",
            swagger_favicon_url="data:,",
        )
        script = bytes(response.body).split(b"<script>", 1)[1].split(b"</script>", 1)[0]
        digest = b64encode(sha256(script).digest()).decode("ascii")
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            f"script-src 'self' https://cdn.jsdelivr.net 'sha256-{digest}'"
        )
        return response

    return router

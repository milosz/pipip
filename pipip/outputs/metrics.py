"""Prometheus metrics for target state and outbound HTTP requests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Response

from pipip.core.retry import http_metrics
from pipip.db.models import Target
from pipip.db.session import Database


def _timestamp(value: datetime | None) -> float:
    if value is None:
        return 0
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()


_TARGET_GAUGES: tuple[tuple[str, str, Callable[[Target], int | float]], ...] = (
    (
        "pipip_target_enabled",
        "Whether the target is enabled.",
        lambda target: int(target.enabled),
    ),
    (
        "pipip_target_check_ok",
        "Whether the last check found a release without errors.",
        lambda target: int(target.status == "ok"),
    ),
    (
        "pipip_target_last_check_timestamp_seconds",
        "Unix time of the last completed check.",
        lambda target: _timestamp(target.last_checked_at),
    ),
    (
        "pipip_target_last_change_timestamp_seconds",
        "Unix time of the last version change.",
        lambda target: _timestamp(target.last_changed_at),
    ),
)


def metrics_router(database: Database) -> APIRouter:
    router = APIRouter()

    @router.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        targets = await database.list_targets()

        lines: list[str] = []
        for metric, description, value in _TARGET_GAUGES:
            lines.extend((f"# HELP {metric} {description}", f"# TYPE {metric} gauge"))
            for target in targets:
                # Target names and source identifiers use a restricted ASCII alphabet.
                labels = f'target="{target.name}",source="{target.source}"'
                lines.append(f"{metric}{{{labels}}} {value(target)}")

        requests, retries = http_metrics()
        for metric, description, counts in (
            ("pipip_http_requests_total", "Outbound HTTP requests, excluding retries.", requests),
            ("pipip_http_retries_total", "Outbound HTTP retry attempts.", retries),
        ):
            lines.extend((f"# HELP {metric} {description}", f"# TYPE {metric} counter"))
            for host in sorted(requests):
                label = host.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
                lines.append(f'{metric}{{host="{label}"}} {counts.get(host, 0)}')

        return Response(
            "\n".join(lines) + "\n",
            media_type="text/plain; version=0.0.4",
            headers={"Cache-Control": "no-store"},
        )

    return router

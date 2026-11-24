from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest
from sqlalchemy import select

from pipip.config import parse_config
from pipip.core.engine import PipipEngine
from pipip.core.retry import request_with_retry
from pipip.db.models import Target, VersionRecord, utcnow
from pipip.db.session import Database
from pipip.outputs.app import create_app


@pytest.fixture
async def dashboard_client(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, datetime]]:
    settings = parse_config(
        {
            "history": {"retention": 23},
            "recent": {"days": 14, "limit": 2},
            "api": {"enabled": True},
            "atom": {"enabled": True},
            "badges": {"enabled": True},
            "database": {"url": f"sqlite:///{tmp_path / 'pipip.db'}"},
            "scheduler": {"default_interval_seconds": 7200, "run_on_start": False},
            "targets": [
                {
                    "name": name,
                    "source": "github" if name == "reference" else "docker",
                    "options": (
                        {"repo": "owner/project"} if name == "reference"
                        else {"image": "library/nginx"}
                    ),
                    "enabled": name != "disabled",
                    "group": {"stale": "Zeta", "current": "Alpha"}.get(name),
                    "hide_in_dashboard": name == "hidden",
                    "interval_seconds": 3600 if name == "stale" else None,
                }
                for name in (
                    "stale", "current", "error", "pending", "no-release", "disabled", "hidden",
                    "reference",
                )
            ],
            "comparisons": [
                {"name": "needs-check", "reference": "current", "targets": ["stale"]},
                {"name": "expected-drift", "reference": "current", "targets": ["reference"]},
            ],
        }
    )
    database = Database(settings.database)
    await database.init()
    try:
        engine = PipipEngine(settings, database)
        await engine.sync_targets()
        now = utcnow()
        async with database.session() as session:
            for target in (await session.scalars(select(Target))).all():
                if target.name == "pending":
                    continue
                if target.name == "stale":
                    session.add(VersionRecord(
                        application_id=target.id, version="0.9.0",
                        detected_at=now - timedelta(days=90), is_current=False,
                        release_url="https://hub.docker.com/_/nginx/tags?name=0.9.0",
                    ))
                    session.add(VersionRecord(
                        application_id=target.id, version="1.0.0", detected_at=now,
                        is_current=True, metadata_json={"labels": {"maintainer": "pi-pip! Team"}},
                        release_url="https://hub.docker.com/_/nginx/tags?name=1.0.0",
                    ))
                target.status = {
                    "error": "error",
                    "disabled": "error",
                    "hidden": "error",
                    "no-release": "no_release",
                }.get(target.name, "ok")
                if target.name == "error":
                    target.last_error = "Registry unavailable"
                target.current_version = None if target.name == "no-release" else "1.0.0"
                if target.name == "reference":
                    target.current_version = "2.0.0"
                    session.add(VersionRecord(
                        application_id=target.id, version="2.0.0",
                        detected_at=now - timedelta(days=90), is_current=True,
                        release_url="javascript:void(0)",
                    ))
                target.last_checked_at = now - timedelta(
                    minutes=10 if target.name == "reference" else 90
                )
                if target.current_version:
                    days = {"stale": 0, "reference": 5, "disabled": 8}.get(target.name, 2)
                    target.last_changed_at = now - timedelta(days=days, hours=1)
                    if target.name == "error":
                        target.last_changed_at = now - timedelta(hours=12)
    finally:
        await database.dispose()

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, now


async def test_dashboard_navigation_and_group_order(
    dashboard_client: tuple[httpx.AsyncClient, datetime],
) -> None:
    client, _ = dashboard_client
    response = await client.get("/")
    assert "/app/current" in response.text
    assert '<th scope="col">Status</th>' not in response.text
    assert "Needs attention (4)" in response.text
    assert response.text.index('class="group-title">Alpha') < response.text.index(
        'class="group-title">Zeta'
    ) < response.text.index('class="group-title">Ungrouped')
    assert "Version activity" not in response.text
    assert 'href="/recent">Recent changes</a>' in response.text
    assert 'class="recent-changes-list"' not in response.text
    assert (await client.get("/changes")).status_code == 404


async def test_recent_changes_page(
    dashboard_client: tuple[httpx.AsyncClient, datetime],
) -> None:
    client, now = dashboard_client
    changes = await client.get("/recent")
    assert changes.status_code == 200
    assert "Up to 2 entries from the last 14 days" in changes.text
    assert '<a href="/app/stale">stale</a>' in changes.text
    assert f'datetime="{now.date().isoformat()}"' in changes.text
    assert now.strftime("%H:%M") in changes.text
    assert "<code>0.9.0</code> →" in changes.text
    assert "<code>1.0.0</code>" in changes.text


async def test_recent_changes_and_history_api(
    dashboard_client: tuple[httpx.AsyncClient, datetime],
) -> None:
    client, now = dashboard_client
    recent = await client.get("/api/recent")
    assert recent.status_code == 200
    (entry,) = recent.json()
    assert entry["target"] == "stale"
    assert entry["previous_version"] == "0.9.0"
    assert entry["version"] == "1.0.0"
    assert datetime.fromisoformat(entry["detected_at"]) == now
    history = await client.get("/api/apps/stale/history", params={"limit": 1})
    assert history.status_code == 200
    assert [release["version"] for release in history.json()] == ["1.0.0"]
    invalid_limit = await client.get("/api/apps/stale/history", params={"limit": 0})
    assert invalid_limit.status_code == 422
    schema = (await client.get("/openapi.json")).json()
    history_parameters = schema["paths"]["/api/apps/{name}/history"]["get"]["parameters"]
    history_limit = next(item for item in history_parameters if item["name"] == "limit")
    assert history_limit["schema"]["default"] == 23


async def test_dashboard_target_details(
    dashboard_client: tuple[httpx.AsyncClient, datetime],
) -> None:
    client, _ = dashboard_client
    detail = await client.get("/app/stale")
    assert detail.status_code == 200
    assert detail.text.count('<span class="status status-current">current</span>') == 2
    assert "Target configuration" not in detail.text
    assert "<h2>Metadata</h2>" in detail.text
    assert '"maintainer": "pi-pip! Team"' in detail.text
    assert detail.text.count('href="https://hub.docker.com/_/nginx/tags?name=1.0.0"') == 1
    assert "View tag on Docker Hub" in detail.text
    assert "https://hub.docker.com/_/nginx/tags?name=0.9.0" not in detail.text
    reference_detail = await client.get("/app/reference")
    assert reference_detail.status_code == 200
    assert "javascript:void(0)" not in reference_detail.text
    assert 'class="release-link"' not in reference_detail.text


async def test_comparisons_page(
    dashboard_client: tuple[httpx.AsyncClient, datetime],
) -> None:
    client, now = dashboard_client
    response = await client.get("/")
    assert "needs-check" not in response.text
    assert "expected-drift" not in response.text
    assert 'href="/comparisons"' in response.text
    comparisons = await client.get("/comparisons")
    assert comparisons.status_code == 200
    assert ">Comparisons</span><strong>2</strong>" in comparisons.text
    assert ">Match</span><strong>1</strong>" in comparisons.text
    assert ">Drift</span><strong>1</strong>" in comparisons.text
    assert ">Missing</span><strong>0</strong>" in comparisons.text
    assert (
        "Latest check · " + (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S UTC")
    ) in comparisons.text
    assert 'id="comparison-expected-drift"' in comparisons.text
    assert "needs-check" in comparisons.text
    assert comparisons.text.index("needs-check") < comparisons.text.index("expected-drift")
    assert 'class="group-title"' not in comparisons.text


async def test_comparisons_api(
    dashboard_client: tuple[httpx.AsyncClient, datetime],
) -> None:
    client, _ = dashboard_client
    result = await client.get("/api/comparisons")
    assert result.status_code == 200
    assert all("group" not in item for item in result.json())
    assert {item["name"]: item["status"] for item in result.json()} == {
        "needs-check": "match", "expected-drift": "drift",
    }
    detail = await client.get("/api/comparisons/expected-drift")
    assert detail.status_code == 200
    assert detail.json() == next(
        item for item in result.json() if item["name"] == "expected-drift"
    )
    assert (await client.get("/api/comparisons/missing")).status_code == 404


async def test_atom_feeds(
    dashboard_client: tuple[httpx.AsyncClient, datetime],
) -> None:
    client, _ = dashboard_client
    feed = await client.get("/feed.atom")
    assert feed.status_code == 200
    assert feed.headers["content-type"].startswith("application/atom+xml")
    assert "stale: 0.9.0 → 1.0.0" in feed.text
    comparison_feed = await client.get("/comparisons.atom")
    assert comparison_feed.status_code == 200
    assert comparison_feed.headers["content-type"].startswith("application/atom+xml")
    atom = {"atom": "http://www.w3.org/2005/Atom"}
    entries = ElementTree.fromstring(comparison_feed.content).findall("atom:entry", atom)
    assert [entry.findtext("atom:title", namespaces=atom) for entry in entries] == [
        "needs-check: Versions match", "expected-drift: Versions differ",
    ]
    content = entries[1].findtext("atom:content", default="", namespaces=atom)
    assert "Reference: current (1.0.0)." in content
    assert "reference (2.0.0): newer than the reference." in content
    assert "http://test/comparisons#comparison-expected-drift" in comparison_feed.text


async def test_badges(
    dashboard_client: tuple[httpx.AsyncClient, datetime],
) -> None:
    client, _ = dashboard_client
    badges = await client.get("/badges")
    assert badges.status_code == 200
    assert 'class="table-card"' in badges.text
    assert "/badge/stale.svg" in badges.text
    badge = await client.get("/badge/stale.svg")
    assert badge.headers["content-type"] == "image/svg+xml"
    assert "1.0.0" in badge.text


async def test_dashboard_attention_filter_uses_target_schedule(
    dashboard_client: tuple[httpx.AsyncClient, datetime],
) -> None:
    client, _ = dashboard_client
    response = await client.get("/", params={"attention": "true"})
    assert response.status_code == 200
    for target in ("stale", "error", "pending", "no-release"):
        assert f"/app/{target}" in response.text
    for target in ("current", "disabled", "hidden"):
        assert f"/app/{target}" not in response.text
    assert "Check overdue" in response.text
    assert '<small class="error-text" title="Registry unavailable">' in response.text


async def test_dashboard_updated_filter(
    dashboard_client: tuple[httpx.AsyncClient, datetime],
) -> None:
    client, _ = dashboard_client
    targets_by_window = {
        "8h": {"stale"},
        "24h": {"stale", "error"},
        "3d": {"stale", "current", "error"},
        "7d": {"stale", "current", "error", "reference"},
    }
    all_targets = set().union(*targets_by_window.values(), {"disabled", "no-release"})
    for window, expected in targets_by_window.items():
        response = await client.get("/", params={"updated": window})
        assert response.status_code == 200
        for target in all_targets:
            assert (f'href="/app/{target}"' in response.text) == (
                target in expected
            )

    invalid = await client.get("/", params={"updated": "unknown"})
    assert invalid.status_code == 422


async def test_dashboard_combines_updated_and_attention_filters(
    dashboard_client: tuple[httpx.AsyncClient, datetime],
) -> None:
    client, _ = dashboard_client
    response = await client.get(
        "/", params={"attention": "true", "updated": "3d"}
    )
    assert '<nav class="target-filters" aria-label="Target filters">' in response.text
    active_filter = '?attention=true" aria-current="page">'
    assert "Version change</span>" not in response.text
    assert re.search(re.escape(active_filter) + r"\s*Needs attention", response.text)
    assert "/app/stale" in response.text
    assert "/app/error" in response.text
    assert "/app/current" not in response.text
    assert "/app/pending" in response.text


@pytest.mark.parametrize("metrics_enabled", [False, True])
async def test_standalone_in_memory_dashboard(metrics_enabled: bool) -> None:
    settings = parse_config({
        "database": {"url": "sqlite:///:memory:"},
        "metrics": {"enabled": metrics_enabled},
        "api": {"enabled": True},
        "atom": {"enabled": True},
        "badges": {"enabled": True},
        "targets": [{"name": "nginx", "source": "docker", "options": {"image": "nginx"}}],
    })
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 204
        dashboard = await client.get("/")
        assert dashboard.status_code == 200
        assert "/app/nginx" in dashboard.text
        detail = await client.get("/app/nginx")
        assert detail.status_code == 200
        assert "nginx" in detail.text
        for path in ("/recent", "/comparisons", "/feed.atom", "/badges", "/badge/nginx.svg"):
            assert (await client.get(path)).status_code == 200
        docs = await client.get("/docs")
        assert docs.status_code == 200
        assert "SwaggerUIBundle" in docs.text
        assert "sha256-" in docs.headers["content-security-policy"]
        schema = await client.get("/openapi.json")
        assert set(schema.json()["paths"]) == {
            "/api/apps", "/api/apps/{name}", "/api/apps/{name}/history",
            "/api/recent", "/api/comparisons", "/api/comparisons/{name}",
        }
        targets = (await client.get("/api/apps")).json()
        assert targets[0]["name"] == "nginx"
        assert "target_config" not in targets[0]
        assert (await client.get("/api/apps/missing")).status_code == 404
        assert (await client.get("/zabbix/lld")).status_code == 404
        if metrics_enabled:
            statuses = iter([503, 200])

            def serve_request(request: httpx.Request) -> httpx.Response:
                return httpx.Response(next(statuses), request=request)

            async with httpx.AsyncClient(transport=httpx.MockTransport(serve_request)) as upstream:
                await request_with_retry(
                    upstream, "GET", "https://metrics-upstream.test/",
                    retry=settings.retry.model_copy(update={"backoff_seconds": 0}),
                    validate_url=False,
                )

        metrics = await client.get("/metrics")
        if metrics_enabled:
            assert metrics.status_code == 200
            assert "version=0.0.4" in metrics.headers["content-type"]
            assert 'pipip_target_enabled{target="nginx",source="docker"} 1' in metrics.text
            assert "# TYPE pipip_http_requests_total counter" in metrics.text
            assert "# TYPE pipip_http_retries_total counter" in metrics.text
            assert 'pipip_http_requests_total{host="metrics-upstream.test"} 1' in metrics.text
            assert 'pipip_http_retries_total{host="metrics-upstream.test"} 1' in metrics.text
        else:
            assert metrics.status_code == 404

@pytest.mark.parametrize("enabled_output", [None, "api", "atom", "badges"])
async def test_optional_outputs_are_independent(enabled_output: str | None) -> None:
    config = {
        "database": {"url": "sqlite:///:memory:"},
        "targets": [{"name": "nginx", "source": "docker", "options": {"image": "nginx"}}],
    }
    if enabled_output is not None:
        config[enabled_output] = {"enabled": True}
    app = create_app(parse_config(config))
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        dashboard = await client.get("/")
        assert dashboard.status_code == 200

        for output, link, extra_paths in (
            ("api", "/docs", ("/openapi.json", "/api/apps", "/api/recent", "/api/comparisons")),
            ("atom", "/feed.atom", ("/comparisons.atom",)),
            ("badges", "/badges", ("/badge/nginx.svg",)),
        ):
            is_enabled = output == enabled_output
            if output == "atom":
                footer = dashboard.text.split("<footer", 1)[1]
                assert ('aria-label="Atom feed"' in footer) is is_enabled
                comparisons = await client.get("/comparisons")
                assert (
                    'href="/comparisons.atom"' in comparisons.text.split("<footer", 1)[1]
                ) is is_enabled
            assert (f'href="{link}"' in dashboard.text) is is_enabled
            for path in (link, *extra_paths):
                response = await client.get(path)
                assert response.status_code == (200 if is_enabled else 404)


async def test_http_root_path_routes_and_links() -> None:
    settings = parse_config({
        "http": {"root_path": "/pipip/"},
        "database": {"url": "sqlite:///:memory:"},
        "scheduler": {"enabled": False},
        "api": {"enabled": True},
        "atom": {"enabled": True},
        "badges": {"enabled": True},
        "metrics": {"enabled": True},
        "targets": [{"name": "nginx", "source": "docker", "options": {"image": "nginx"}}],
    })
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        dashboard = await client.get("/pipip/")
        assert dashboard.status_code == 200
        for link in ("app/nginx", "recent", "comparisons", "docs", "feed.atom", "badges"):
            assert f'href="/pipip/{link}"' in dashboard.text
        assert (await client.get("/")).status_code == 404
        assert (await client.get("/pipip-extra/")).status_code == 404
        assert (await client.get("/pipip", follow_redirects=True)).status_code == 200
        for link in (
            "app/nginx", "recent", "comparisons", "feed.atom", "comparisons.atom",
            "badge/nginx.svg", "metrics",
        ):
            assert (await client.get(f"/pipip/{link}")).status_code == 200
            assert (await client.get(f"/{link}")).status_code == 404
        docs = await client.get("/pipip/docs")
        assert "/pipip/openapi.json" in docs.text
        schema = await client.get("/pipip/openapi.json")
        assert schema.json()["servers"] == [{"url": "/pipip"}]
        assert (await client.get("/pipip/health")).status_code == 204
        assert (await client.get("/health")).status_code == 204

from __future__ import annotations

from datetime import UTC, datetime, tzinfo

import httpx
import pytest

from argus.config import RetrySettings
from argus.core import retry as retry_module
from argus.core.retry import request_with_retry


def _settings(**kwargs: object) -> RetrySettings:
    defaults: dict[str, object] = {
        "attempts": 3,
        "backoff_seconds": 0,
        "max_backoff_seconds": 0,
        "retry_statuses": [429, 500, 503],
    }
    defaults.update(kwargs)
    return RetrySettings(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_retries_transient_status_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, request=request)
        return httpx.Response(200, text="ok", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(
            client,
            "GET",
            "https://example.test/",
            retry=_settings(),
            validate_url=False,
            sleep=lambda _: _none(),
        )
    assert response.status_code == 200
    assert calls == 3


async def _none() -> None:
    return None


@pytest.mark.asyncio
async def test_does_not_retry_non_transient_status() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(
            client,
            "GET",
            "https://example.test/",
            retry=_settings(),
            validate_url=False,
            sleep=lambda _: _none(),
        )
    assert response.status_code == 404
    assert calls == 1


@pytest.mark.asyncio
async def test_retries_transport_error_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(
            client,
            "GET",
            "https://example.test/",
            retry=_settings(attempts=2),
            validate_url=False,
            sleep=lambda _: _none(),
        )
    assert response.status_code == 200
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "expected_delay"),
    [
        ("2", 2),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 2),
        ("Wed, 21 Oct 2015 07:27:00 GMT", 0),
        ("Wed, 21 Oct 2015 07:29:00 GMT", 30),
        ("invalid", 0),
        ("NaN", 0),
    ],
)
async def test_honours_retry_after_header(
    header: str, expected_delay: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return datetime(2015, 10, 21, 7, 27, 58, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(retry_module, "datetime", FixedClock)
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429, headers={"retry-after": header}, request=request
            )
        return httpx.Response(200, request=request)

    async def record(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(
            client,
            "GET",
            "https://example.test/",
            retry=_settings(max_backoff_seconds=30),
            validate_url=False,
            sleep=record,
        )
    assert response.status_code == 200
    assert delays == [expected_delay]


@pytest.mark.asyncio
async def test_returns_last_response_after_exhaustion() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(
            client,
            "GET",
            "https://example.test/",
            retry=_settings(attempts=2),
            validate_url=False,
            sleep=lambda _: _none(),
        )
    assert response.status_code == 503
    assert calls == 2

"""Async HTTP request helper with retry and exponential backoff."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any
from urllib.parse import urlsplit

import httpx

from pipip.config import RetrySettings
from pipip.core.net import ensure_url_allowed

Sleep = Callable[[float], Awaitable[None]]

_REQUEST_COUNTS: dict[str, int] = {}
_RETRY_COUNTS: dict[str, int] = {}


def http_metrics() -> tuple[dict[str, int], dict[str, int]]:
    """Return request/retry counters keyed by host."""

    return dict(_REQUEST_COUNTS), dict(_RETRY_COUNTS)


def _host(url: str) -> str:
    return urlsplit(url).hostname or ""


def _retry_after(response: httpx.Response, settings: RetrySettings) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            date = parsedate_to_datetime(raw)
        except (ValueError, OverflowError):
            return None
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        seconds = (date - datetime.now(UTC)).total_seconds()
    if not isfinite(seconds):
        return None
    return min(max(seconds, 0.0), settings.max_backoff_seconds)


def _delay(settings: RetrySettings, attempt: int) -> float:
    base = settings.backoff_seconds * (2**attempt)
    if base <= 0:
        return 0.0
    capped = min(base, settings.max_backoff_seconds)
    return float(capped * (0.75 + 0.25 * random.random()))


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retry: RetrySettings,
    sleep: Sleep = asyncio.sleep,
    allow_private: bool = False,
    validate_url: bool = True,
    **kwargs: Any,
) -> httpx.Response:
    """Perform a request, retrying transient failures and configured statuses."""

    if validate_url:
        await ensure_url_allowed(url, allow_private=allow_private)

    host = _host(url)
    _REQUEST_COUNTS[host] = _REQUEST_COUNTS.get(host, 0) + 1

    for attempt in range(retry.attempts):
        last_attempt = attempt + 1 >= retry.attempts
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError:
            if last_attempt:
                raise
            _RETRY_COUNTS[host] = _RETRY_COUNTS.get(host, 0) + 1
            await sleep(_delay(retry, attempt))
            continue

        if (
            response.status_code in retry.retry_statuses
            and not last_attempt
        ):
            _RETRY_COUNTS[host] = _RETRY_COUNTS.get(host, 0) + 1
            delay = _retry_after(response, retry)
            await sleep(delay if delay is not None else _delay(retry, attempt))
            continue
        return response

    raise RuntimeError("retry loop exited without a response")

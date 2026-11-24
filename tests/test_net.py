from __future__ import annotations

import pytest

from argus.core.net import UrlNotAllowed, ensure_url_allowed


@pytest.mark.asyncio
async def test_rejects_non_http_scheme() -> None:
    with pytest.raises(UrlNotAllowed):
        await ensure_url_allowed("file:///etc/passwd")


@pytest.mark.asyncio
async def test_rejects_loopback_and_metadata() -> None:
    with pytest.raises(UrlNotAllowed):
        await ensure_url_allowed("http://127.0.0.1/")
    with pytest.raises(UrlNotAllowed):
        await ensure_url_allowed("http://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_allows_public_address() -> None:
    await ensure_url_allowed("https://1.1.1.1/")


@pytest.mark.asyncio
async def test_allow_private_bypasses_address_policy() -> None:
    await ensure_url_allowed("http://127.0.0.1/", allow_private=True)

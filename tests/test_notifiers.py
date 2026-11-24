from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from argus.config import parse_config
from argus.core.types import Notification
from argus.notifiers.ntfy import NtfyNotifier


@pytest.mark.parametrize("destination", [{"server": "https://ntfy.test"}, {"topic": "my-topic"}])
def test_ntfy_requires_a_server_and_topic_when_enabled(destination: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="ntfy requires both server and topic"):
        parse_config({"notifiers": {"ntfy": {"enabled": True, **destination}}})


@pytest.mark.asyncio
@pytest.mark.parametrize(("priority", "expected_priority"), [(None, 3), ("high", 4), (5, 5)])
async def test_ntfy_posts_json_to_server_root(
    monkeypatch: pytest.MonkeyPatch, priority: str | int | None, expected_priority: int
) -> None:
    ntfy: dict[str, str | int] = {"server": "https://example.test", "topic": "argus"}
    if priority is not None:
        ntfy["priority"] = priority
    settings = parse_config({"notifiers": {"ntfy": ntfy}})
    notifier = NtfyNotifier(settings)
    notification = Notification("nginx", "docker", "1.0.0", "1.1.0")

    async def request(client, method, url, **kwargs):
        assert method == "POST"
        assert url == "https://example.test/"
        assert kwargs["json"]["topic"] == "argus"
        assert kwargs["json"]["priority"] == expected_priority
        assert kwargs["json"]["title"] == "nginx: new version detected"
        assert kwargs["json"]["message"] == "1.0.0 → 1.1.0"
        return httpx.Response(200, request=httpx.Request(method, url))

    monkeypatch.setattr(notifier, "request", request)
    await notifier.send(notification)


@pytest.mark.parametrize("token", ["", "test-token"])
async def test_ntfy_prefers_token_to_basic_auth(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    settings = parse_config({
        "notifiers": {"ntfy": {"token": token, "username": "argus", "password": "test-password"}}
    })
    notifier = NtfyNotifier(settings)

    async def request(client, method, url, **kwargs):
        if token:
            assert kwargs["headers"]["Authorization"] == f"Bearer {token}"
            assert kwargs["auth"] is None
        else:
            assert "Authorization" not in kwargs["headers"]
            assert isinstance(kwargs["auth"], httpx.BasicAuth)
        return httpx.Response(200, request=httpx.Request(method, url))

    monkeypatch.setattr(notifier, "request", request)
    await notifier.send(Notification("nginx", "docker", "1.0.0", "1.1.0"))

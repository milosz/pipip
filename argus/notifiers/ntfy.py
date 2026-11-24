"""ntfy.sh / self-hosted ntfy notification channel."""

from __future__ import annotations

import logging

import httpx

from argus.config import AppConfig
from argus.core.plugins import register_notifier
from argus.core.types import Notification
from argus.core.utils import truncate
from argus.notifiers.base import NotificationError, Notifier

logger = logging.getLogger(__name__)
_RELEASE_NOTES_LIMIT = 1_200


@register_notifier("ntfy")
class NtfyNotifier(Notifier):
    """Publish notifications through the ntfy HTTP API."""

    name = "ntfy"

    def __init__(self, settings: AppConfig) -> None:
        super().__init__(settings)
        self.config = settings.notifiers.ntfy

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "argus-release-monitor",
        }
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token.get_secret_value()}"
        return headers

    def _payload(self, notification: Notification) -> dict[str, object]:
        message = notification.message
        if notification.release_url:
            message = f"{message}\n{notification.release_url}"
        if notes := truncate(notification.release_notes, _RELEASE_NOTES_LIMIT):
            message = f"{message}\n\n{notes}"
        payload: dict[str, object] = {
            "topic": self.config.topic.lstrip("/"),
            "title": notification.title,
            "message": message,
            "priority": self.config.priority,
            "tags": self.config.tags,
        }
        if notification.release_url:
            payload["click"] = notification.release_url
        return payload

    async def send(self, notification: Notification) -> None:
        auth: httpx.BasicAuth | None = None
        if not self.config.token and self.config.username and self.config.password:
            auth = httpx.BasicAuth(
                self.config.username, self.config.password.get_secret_value()
            )
        url = f"{self.config.server.rstrip('/')}/"
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await self.request(
                    client,
                    "POST",
                    url,
                    json=self._payload(notification),
                    headers=self._headers(),
                    auth=auth,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise NotificationError(
                f"ntfy returned HTTP {error.response.status_code}: "
                f"{truncate(error.response.text, 300)}"
            ) from error
        except httpx.HTTPError as error:
            raise NotificationError(f"ntfy request failed: {error}") from error
        logger.debug("sent ntfy notification for %s", notification.app_name)

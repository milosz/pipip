"""Abstract notification channel interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import httpx

from argus.config import AppConfig
from argus.core.retry import request_with_retry
from argus.core.types import Notification


class NotificationError(RuntimeError):
    """Raised when a notifier cannot deliver a message."""


class Notifier(ABC):
    """Base class for notification channel plugins."""

    name: ClassVar[str]

    def __init__(self, settings: AppConfig) -> None:
        self.settings = settings

    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        return await request_with_retry(
            client,
            method,
            url,
            retry=self.settings.retry,
            allow_private=self.settings.security.allow_private_networks,
            **kwargs,
        )

    @abstractmethod
    async def send(self, notification: Notification) -> None:
        """Deliver a notification or raise :class:`NotificationError`."""

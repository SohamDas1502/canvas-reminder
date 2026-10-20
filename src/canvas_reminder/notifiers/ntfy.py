"""ntfy push notifications (https://ntfy.sh). The topic name is the secret."""

from __future__ import annotations

from typing import Any

import requests

from ..http_client import request
from ..models import Message
from .base import PushNotifier


class NtfyNotifier(PushNotifier):
    name = "ntfy"

    def __init__(
        self, session: requests.Session, server: str, topic: str, token: str | None = None
    ) -> None:
        self._session = session
        self._server = server.rstrip("/")
        self._topic = topic
        self._token = token

    def send(self, message: Message) -> None:
        # JSON publishing (POST to the server root) handles Unicode titles, unlike headers.
        payload: dict[str, Any] = {
            "topic": self._topic,
            "title": message.title,
            "message": message.body,
            "tags": list(message.tags),
            "priority": message.priority,
        }
        if message.click:
            payload["click"] = message.click
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        request(self._session, "POST", self._server, json=payload, headers=headers)

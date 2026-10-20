"""Refresh-token -> access-token exchange shared by the Google and Microsoft channels."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import requests

from ..http_client import request
from ..redact import register_secret

EXPIRY_MARGIN = 60  # refresh a minute early


class OAuthToken:
    def __init__(
        self,
        session: requests.Session,
        token_url: str,
        params: dict[str, str],
        on_rotated: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`params` holds client_id, refresh_token and any provider extras (secret, scope)."""
        self._session = session
        self._url = token_url
        self._params = params
        self._on_rotated = on_rotated
        self._clock = clock
        self._access: str | None = None
        self._expires_at = 0.0

    def headers(self) -> dict[str, str]:
        if self._access is None or self._clock() >= self._expires_at:
            self._refresh()
        return {"Authorization": f"Bearer {self._access}"}

    def _refresh(self) -> None:
        data = {**self._params, "grant_type": "refresh_token"}
        body: dict[str, Any] = request(self._session, "POST", self._url, data=data).json()
        self._access = str(body["access_token"])
        register_secret(self._access)
        self._expires_at = self._clock() + int(body.get("expires_in", 3600)) - EXPIRY_MARGIN

        rotated = body.get("refresh_token")
        if rotated and rotated != self._params["refresh_token"]:
            register_secret(rotated)
            self._params["refresh_token"] = rotated
            if self._on_rotated:
                self._on_rotated(rotated)

"""HTTP with retries and backoff, and errors that never contain a URL.

`requests` puts the full URL in its exception messages (e.g. "404 Client Error ... for url:
https://...").  For the Canvas feed and ntfy that URL *is* the secret, so every call goes
through `request()`, which re-raises as `HttpError` naming only the host.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_TIMEOUT = 20  # seconds


class HttpError(Exception):
    """An HTTP call failed. `status` is None when no response was received."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def make_session(retries: int = 3, backoff: float = 1.0) -> requests.Session:
    """Session that retries connection errors, 429 and 5xx with exponential backoff.

    Status retries apply only to idempotent methods (GET/PUT/DELETE...), so a POST that
    reached the server is never silently repeated.
    """
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = "canvas-reminder-pipeline/0.1"
    return session


def request(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
    """Send a request; raise `HttpError` (host only, no path or query) on any failure."""
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    where = f"{method} {urlsplit(url).hostname}"
    try:
        response = session.request(method, url, **kwargs)
    except requests.RequestException as exc:
        raise HttpError(f"{where} failed ({type(exc).__name__})") from None
    if response.status_code >= 400:
        raise HttpError(f"{where} returned HTTP {response.status_code}", response.status_code)
    return response

"""Keep secrets out of logs.

Two layers: every secret value loaded at runtime is registered and replaced verbatim,
and generic patterns (URLs, bearer tokens, token fields) are masked as a safety net.
"""

from __future__ import annotations

import logging
import re
import traceback
from urllib.parse import urlsplit

REDACTED = "[REDACTED]"

_secrets: set[str] = set()
_URL = re.compile(r"https?://[^\s'\"<>]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_TOKEN_FIELD = re.compile(
    r"""(?i)(["']?(?:access_token|refresh_token|client_secret|token|password)["']?\s*[:=]\s*["']?)"""
    r"""[^"'\s,&}]+"""
)
_CANVAS_FEED = re.compile(r"feeds/calendars/user_[A-Za-z0-9]+")


def register_secret(value: str) -> None:
    """Mask this exact value (and, for a URL, its path) wherever it appears in a log line."""
    if len(value) < 6:
        return
    _secrets.add(value)
    parts = urlsplit(value)
    if parts.scheme and parts.path not in ("", "/"):
        _secrets.add(parts.path + (f"?{parts.query}" if parts.query else ""))


def redact(text: str) -> str:
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = _CANVAS_FEED.sub(REDACTED, text)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    text = _TOKEN_FIELD.sub(rf"\1{REDACTED}", text)
    return _URL.sub(_mask_url, text)


def _mask_url(match: re.Match[str]) -> str:
    parts = urlsplit(match.group(0))
    has_detail = parts.path not in ("", "/") or parts.query
    return f"{parts.scheme}://{parts.hostname}/{REDACTED}" if has_detail else match.group(0)


class RedactingFilter(logging.Filter):
    """Rewrites each record (message, args and traceback) through `redact()`."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(message)
        record.args = None
        return True


def setup_logging(level: str = "INFO") -> None:
    """Attach the redacting filter to every root handler (Lambda installs its own)."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")
    root.setLevel(level)
    # urllib3 logs request paths at DEBUG; a feed path is a secret.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())

"""Channel registry: builds only the notifiers enabled in config.yaml.

Secrets are fetched only for enabled channels, so a disabled channel needs no secret.
A channel that can't be set up (e.g. its secret is missing) is skipped and reported,
so one misconfigured channel never stops the others.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import requests

from ..config import Config
from ..secrets import SecretStore
from . import gcal, mstodo
from .base import Notifier, PushNotifier, ReminderNotifier
from .gcal import GoogleCalendarNotifier
from .mstodo import MicrosoftTodoNotifier
from .ntfy import NtfyNotifier
from .oauth import OAuthToken

__all__ = ["Notifier", "PushNotifier", "ReminderNotifier", "build_notifiers"]

logger = logging.getLogger(__name__)


def build_notifiers(
    config: Config,
    secrets: SecretStore,
    session: requests.Session,
    errors: list[str] | None = None,
) -> list[Notifier]:
    """Build enabled channels. Setup failures are appended to `errors` (and logged)."""

    def ntfy() -> Notifier:
        value = secrets.get_json("ntfy")
        return NtfyNotifier(session, config.ntfy_server, value["topic"], value.get("token"))

    def google() -> Notifier:
        value = secrets.get_json("google")
        params = {k: value[k] for k in ("client_id", "client_secret", "refresh_token")}
        token = OAuthToken(session, gcal.TOKEN_URL, params)
        return GoogleCalendarNotifier(session, config.google_calendar_id, token)

    def microsoft() -> Notifier:
        value = secrets.get_json("microsoft")
        params = {
            "client_id": value["client_id"],
            "refresh_token": value["refresh_token"],
            "scope": mstodo.SCOPES,
        }

        def save_rotated(refresh_token: str) -> None:
            # Microsoft rotates refresh tokens on use; persist the new one.
            secrets.put_json("microsoft", {**value, "refresh_token": refresh_token})

        token = OAuthToken(session, mstodo.TOKEN_URL, params, on_rotated=save_rotated)
        return MicrosoftTodoNotifier(session, config.todo_list_name, token)

    builders: dict[str, Callable[[], Notifier]] = {
        "ntfy": ntfy,
        "gcal": google,
        "mstodo": microsoft,
    }
    notifiers: list[Notifier] = []
    for channel, build in builders.items():
        if channel not in config.channels:
            continue
        try:
            notifiers.append(build())
        except Exception as exc:
            message = f"{channel} skipped: could not set it up ({type(exc).__name__}: {exc})"
            logger.error(message)
            if errors is not None:
                errors.append(message)
    return notifiers

"""Apple Reminders feed: a small read-only JSON endpoint that an iPhone Shortcut polls.

Apple has no server-side API for Reminders, so the pipeline cannot push to it. Instead
a Shortcut automation fetches this feed a few times a day and creates or updates one
reminder per assignment (matched by the `canvas-id` line in the reminder's notes).

Reminders get one alert each, so `alert` is the *next* upcoming lead time: with
[2d, 2h] it is 2 days before until that passes, then 2 hours before.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import boto3

from .config import Config, load_config
from .models import Status, StoredAssignment
from .redact import setup_logging
from .schedule import LOCAL_TZ, format_local, next_lead
from .secrets import SecretStore
from .state import StateRepository

logger = logging.getLogger(__name__)
CHANNEL = "apple"
_cached: tuple[Config, StateRepository, SecretStore] | None = None


def canvas_tag(uid: str) -> str:
    """The line the Shortcut searches reminder notes for."""
    return f"canvas-id: {uid}"


def build_feed(
    records: Iterable[StoredAssignment], config: Config, now: datetime
) -> dict[str, Any]:
    items = []
    for record in sorted(records, key=lambda r: (r.due, r.uid)):
        if record.due <= now or config.is_ignored(record.course):
            continue
        alert = record.due - next_lead(record.due, config.leads_for(record.course), now)
        notes = "\n".join(part for part in (record.url, canvas_tag(record.uid)) if part)
        items.append(
            {
                "id": record.uid,
                "tag": canvas_tag(record.uid),
                "title": record.label,
                "due": record.due.astimezone(LOCAL_TZ).isoformat(),
                "due_text": format_local(record.due),
                "alert": alert.astimezone(LOCAL_TZ).isoformat(),
                "alert_text": format_local(alert),
                "notes": notes,
                # Text, not a boolean, so the Shortcut can compare it in an "If" action.
                # Mirrors on_removed: only "delete" removes the reminder from your phone.
                "action": "delete" if _deleted(record, config) else "add-or-update",
            }
        )
    return {"generated": now.astimezone(LOCAL_TZ).isoformat(), "items": items}


def _deleted(record: StoredAssignment, config: Config) -> bool:
    return record.status is Status.REMOVED and config.on_removed == "delete"


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda Function URL handler. Requires `Authorization: Bearer <token>`."""
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    config, repo, secrets = _deps()
    if CHANNEL not in config.channels:
        return _response(404, {"error": "Apple Reminders is not enabled in config.yaml"})

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    supplied = headers.get("authorization", "").removeprefix("Bearer ").strip()
    expected = str(secrets.get_json(CHANNEL)["token"])
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        logger.warning("Rejected Apple feed request with a missing or wrong token")
        return _response(401, {"error": "unauthorised"})

    feed = build_feed(repo.load_all().values(), config, datetime.now(UTC))
    logger.info("Served Apple feed with %d items", len(feed["items"]))
    return _response(200, feed)


def _deps() -> tuple[Config, StateRepository, SecretStore]:
    global _cached
    if _cached is None:
        dynamodb = boto3.resource(
            "dynamodb", endpoint_url=os.environ.get("DYNAMODB_ENDPOINT_URL") or None
        )
        _cached = (
            load_config(),
            StateRepository(dynamodb.Table(os.environ["TABLE_NAME"])),
            SecretStore(prefix=os.environ.get("SECRET_PREFIX", "canvas-reminder")),
        )
    return _cached


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body),
    }

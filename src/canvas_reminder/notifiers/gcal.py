"""Google Calendar: one event per assignment at its due time, with popup reminders.

The event id is derived from the Canvas UID, so creating it is naturally idempotent:
even if a run crashes after Google accepted the event, the next run PUTs the same id.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import requests

from ..http_client import HttpError, request
from ..models import StoredAssignment
from ..schedule import LOCAL_TZ
from .base import ReminderNotifier
from .oauth import OAuthToken

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, not a secret
API = "https://www.googleapis.com/calendar/v3"
GONE = (404, 410)


def event_id_for(uid: str) -> str:
    """Google event ids must use base32hex characters (a-v, 0-9); hex digits qualify."""
    return "canvas" + hashlib.sha256(uid.encode()).hexdigest()[:32]


class GoogleCalendarNotifier(ReminderNotifier):
    name = "gcal"

    def __init__(self, session: requests.Session, calendar_id: str, token: OAuthToken) -> None:
        self._session = session
        self._events = f"{API}/calendars/{quote(calendar_id, safe='')}/events"
        self._token = token

    def upsert(
        self, assignment: StoredAssignment, leads: Sequence[timedelta], ref: str | None
    ) -> str:
        event_id = ref or event_id_for(assignment.uid)
        body = self._event_body(assignment, leads) | {"id": event_id}
        try:
            # PUT updates in place, and also restores an event the user deleted.
            self._call("PUT", f"{self._events}/{event_id}", json=body)
        except HttpError as exc:
            if exc.status not in GONE:
                raise
            self._call("POST", self._events, json=body)
        return event_id

    def delete(self, ref: str) -> None:
        try:
            self._call("DELETE", f"{self._events}/{ref}")
        except HttpError as exc:
            if exc.status not in GONE:
                raise

    def _call(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        return request(self._session, method, url, headers=self._token.headers(), **kwargs)

    @staticmethod
    def _event_body(a: StoredAssignment, leads: Sequence[timedelta]) -> dict[str, Any]:
        moment = {"dateTime": a.due.astimezone(LOCAL_TZ).isoformat(), "timeZone": str(LOCAL_TZ)}
        description = "\n\n".join(part for part in (a.description, a.url) if part)
        return {
            "summary": f"Due: {a.label}",
            "description": description,
            "start": moment,
            "end": moment,
            "status": "confirmed",
            "transparency": "transparent",  # don't show as busy
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": int(lead.total_seconds() // 60)}
                    for lead in leads
                ],
            },
            "extendedProperties": {"private": {"canvasUid": a.uid}},
        }

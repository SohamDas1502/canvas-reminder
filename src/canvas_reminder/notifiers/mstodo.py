"""Microsoft To Do (via Microsoft Graph): one task per assignment with a due date and reminder.

Graph has no client-chosen ids, so each task carries a linked resource whose externalId
is the Canvas UID. Before creating a task we look for one with that externalId, which
makes creation idempotent even if a run crashed after Graph accepted the task.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any

import requests

from ..http_client import HttpError, request
from ..models import StoredAssignment
from ..schedule import LOCAL_TZ
from .base import ReminderNotifier
from .oauth import OAuthToken

# Personal Microsoft accounts use the "consumers" tenant.
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"  # noqa: S105 - endpoint, not a secret
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "Tasks.ReadWrite offline_access"
APP_NAME = "canvas-reminder-pipeline"
# Graph wants Windows time zone names; this one covers Melbourne, including daylight saving.
GRAPH_TZ = "AUS Eastern Standard Time"


def graph_time(moment: datetime) -> dict[str, str]:
    local = moment.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return {"dateTime": local.isoformat(timespec="seconds"), "timeZone": GRAPH_TZ}


class MicrosoftTodoNotifier(ReminderNotifier):
    name = "mstodo"
    single_alert = True

    def __init__(self, session: requests.Session, list_name: str, token: OAuthToken) -> None:
        self._session = session
        self._list_name = list_name
        self._token = token
        self._list_id: str | None = None

    def upsert(
        self, assignment: StoredAssignment, leads: Sequence[timedelta], ref: str | None
    ) -> str:
        body = self._task_body(assignment, leads)
        task_id = ref or self._find_task(assignment.uid)
        if task_id:
            try:
                self._call("PATCH", f"{self._tasks_url()}/{task_id}", json=body)
                return task_id
            except HttpError as exc:
                if exc.status != 404:
                    raise  # 404: the user deleted it; create a fresh one below
        body["linkedResources"] = [
            {
                "applicationName": APP_NAME,
                "externalId": assignment.uid,
                "displayName": assignment.label,
                **({"webUrl": assignment.url} if assignment.url else {}),
            }
        ]
        return str(self._call("POST", self._tasks_url(), json=body).json()["id"])

    def delete(self, ref: str) -> None:
        try:
            self._call("DELETE", f"{self._tasks_url()}/{ref}")
        except HttpError as exc:
            if exc.status != 404:
                raise

    # --- helpers -----------------------------------------------------------------------

    def _find_task(self, uid: str) -> str | None:
        url = f"{self._tasks_url()}?$expand=linkedResources"
        for task in self._paged(url):
            if any(r.get("externalId") == uid for r in task.get("linkedResources", [])):
                return str(task["id"])
        return None

    def _tasks_url(self) -> str:
        if self._list_id is None:
            self._list_id = self._find_or_create_list()
        return f"{GRAPH}/me/todo/lists/{self._list_id}/tasks"

    def _find_or_create_list(self) -> str:
        for todo_list in self._paged(f"{GRAPH}/me/todo/lists"):
            if todo_list.get("displayName") == self._list_name:
                return str(todo_list["id"])
        created = self._call(
            "POST", f"{GRAPH}/me/todo/lists", json={"displayName": self._list_name}
        )
        return str(created.json()["id"])

    def _paged(self, url: str | None) -> Iterator[dict[str, Any]]:
        while url:
            page = self._call("GET", url).json()
            yield from page.get("value", [])
            url = page.get("@odata.nextLink")

    def _call(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        return request(self._session, method, url, headers=self._token.headers(), **kwargs)

    @staticmethod
    def _task_body(a: StoredAssignment, leads: Sequence[timedelta]) -> dict[str, Any]:
        # To Do allows one reminder per task; the pipeline passes the next upcoming lead.
        content = "\n\n".join(part for part in (a.description, a.url) if part)
        return {
            "title": a.label,
            "body": {"content": content, "contentType": "text"},
            "dueDateTime": graph_time(a.due),
            "isReminderOn": True,
            "reminderDateTime": graph_time(a.due - max(leads)),
        }

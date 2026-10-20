"""In-memory channels for pipeline tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from canvas_reminder.http_client import HttpError
from canvas_reminder.models import Message, StoredAssignment
from canvas_reminder.notifiers import PushNotifier, ReminderNotifier


class FakePush(PushNotifier):
    name = "ntfy"

    def __init__(self) -> None:
        self.sent: list[Message] = []
        self.fail_with: Exception | None = None

    def send(self, message: Message) -> None:
        if self.fail_with:
            raise self.fail_with
        self.sent.append(message)

    @property
    def titles(self) -> list[str]:
        return [m.title for m in self.sent]


class FakeCalendar(ReminderNotifier):
    name = "gcal"

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.creates = 0
        self.updates = 0
        self.deleted: list[str] = []
        self.fail = False

    def upsert(
        self, assignment: StoredAssignment, leads: Sequence[timedelta], ref: str | None
    ) -> str:
        if self.fail:
            raise HttpError("PUT calendar returned HTTP 503", 503)
        if ref is None:
            self.creates += 1
            ref = f"evt{self.creates}"
        else:
            self.updates += 1
        self.events[ref] = {"uid": assignment.uid, "due": assignment.due, "leads": tuple(leads)}
        return ref

    def delete(self, ref: str) -> None:
        self.deleted.append(ref)
        self.events.pop(ref, None)


class FakeTodo(FakeCalendar):
    """Like To Do: one alert per item, so the pipeline passes only the next lead."""

    name = "mstodo"
    single_alert = True

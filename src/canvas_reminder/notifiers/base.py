"""The common interface every channel implements.

Channels come in two shapes:

* PushNotifier (ntfy): delivers a message now. The pipeline decides *when* to push
  (new/changed/removed events, and due-soon reminders at each lead time).
* ReminderNotifier (Google Calendar, Microsoft To Do): owns one object per assignment
  and lets that service fire the reminder at the right time. The pipeline creates
  it once and updates it in place when the assignment changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import timedelta
from typing import ClassVar

from ..models import Message, StoredAssignment


class Notifier(ABC):
    name: ClassVar[str]


class PushNotifier(Notifier):
    @abstractmethod
    def send(self, message: Message) -> None: ...


class ReminderNotifier(Notifier):
    # True for services that allow one alert per item (Microsoft To Do). The pipeline
    # then passes only the next upcoming lead and moves the alert along as each fires.
    single_alert: ClassVar[bool] = False

    @abstractmethod
    def upsert(
        self, assignment: StoredAssignment, leads: Sequence[timedelta], ref: str | None
    ) -> str:
        """Create the reminder, or update the one identified by `ref`. Returns its id."""

    @abstractmethod
    def delete(self, ref: str) -> None:
        """Delete the reminder. Must succeed if it is already gone."""

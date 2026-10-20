"""Domain types shared by every stage of the pipeline. Pure data, no I/O."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Status(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"


@dataclass(frozen=True)
class Assignment:
    """An assignment as it appears in the Canvas feed right now. `due` is timezone-aware UTC."""

    uid: str
    title: str
    course: str
    due: datetime
    description: str = ""
    url: str | None = None

    @property
    def content_hash(self) -> str:
        """Fingerprint of the fields whose change should update a reminder."""
        payload = "\x1f".join((self.title, self.due.isoformat(), self.description))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class StoredAssignment:
    """An assignment as recorded in DynamoDB, plus what has already been done about it.

    `sent` holds claim keys for push messages already delivered; `refs` maps a reminder
    channel to {"ref": <id in that service>, "hash": <content_hash it was synced at>}.
    """

    uid: str
    title: str
    course: str
    due: datetime
    content_hash: str
    version: int
    status: Status = Status.ACTIVE
    description: str = ""
    url: str | None = None
    previous_due: datetime | None = None
    sent: frozenset[str] = frozenset()
    refs: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.title} [{self.course}]" if self.course else self.title


@dataclass(frozen=True)
class Message:
    """A push notification, independent of the channel that delivers it."""

    title: str
    body: str
    tags: tuple[str, ...] = ()
    priority: int = 3
    click: str | None = None

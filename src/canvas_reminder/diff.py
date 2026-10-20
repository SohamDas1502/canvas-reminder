"""Compare the feed with stored state. Pure: no I/O, so every case is unit-tested."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .models import Assignment, Status, StoredAssignment


class ChangeKind(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    REMOVED = "removed"


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    uid: str
    current: Assignment | None = None
    previous: StoredAssignment | None = None


def diff_assignments(
    current: Iterable[Assignment],
    previous: Mapping[str, StoredAssignment],
    now: datetime,
) -> list[Change]:
    """Classify every assignment as new, changed, unchanged or removed.

    An assignment that comes back after being removed counts as changed. Something
    missing from the feed counts as removed only if it was still upcoming: Canvas
    drops old events from the feed, and those have not been "removed".
    """
    changes: list[Change] = []
    seen: set[str] = set()
    for assignment in current:
        seen.add(assignment.uid)
        before = previous.get(assignment.uid)
        if before is None:
            kind = ChangeKind.NEW
        elif before.status is Status.REMOVED or before.content_hash != assignment.content_hash:
            kind = ChangeKind.CHANGED
        else:
            kind = ChangeKind.UNCHANGED
        changes.append(Change(kind, assignment.uid, assignment, before))

    for uid, before in previous.items():
        if uid not in seen and before.status is Status.ACTIVE and before.due > now:
            changes.append(Change(ChangeKind.REMOVED, uid, None, before))
    return changes


def looks_truncated(
    parsed: Iterable[Assignment], previous: Mapping[str, StoredAssignment], now: datetime
) -> bool:
    """True if the feed is empty while upcoming assignments are on record.

    That is almost certainly a Canvas or network glitch, not every assignment being
    deleted at once, so the run should stop instead of reporting them all as removed.
    """
    return not any(True for _ in parsed) and any(
        r.status is Status.ACTIVE and r.due > now for r in previous.values()
    )

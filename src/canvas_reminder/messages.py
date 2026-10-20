"""Text of push notifications. Pure."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from .models import Message, Status, StoredAssignment
from .schedule import format_local, humanize


def _when(record: StoredAssignment, now: datetime) -> str:
    return f"Due {format_local(record.due)} (in {humanize(record.due - now)})"


def event_message(record: StoredAssignment, now: datetime) -> Message:
    """Announce a new or changed assignment."""
    body = _when(record, now)
    if record.version == 1:
        title, tags = f"New: {record.title}", ("new",)
    elif record.previous_due is not None and record.previous_due != record.due:
        title, tags = f"Due date changed: {record.title}", ("calendar",)
        body += f"\nWas {format_local(record.previous_due)}"
    else:
        title, tags = f"Updated: {record.title}", ("pencil",)
    return Message(_with_course(title, record), body, tags, click=record.url)


def due_soon_message(record: StoredAssignment, now: datetime) -> Message:
    title = f"Due in {humanize(record.due - now)}: {record.title}"
    body = f"Due {format_local(record.due)}"
    return Message(
        _with_course(title, record), body, ("alarm_clock",), priority=4, click=record.url
    )


def removed_message(record: StoredAssignment) -> Message:
    body = f"No longer in your Canvas calendar (was due {format_local(record.due)})."
    return Message(_with_course(f"Removed: {record.title}", record), body, ("wastebasket",))


def bootstrap_message(records: Sequence[StoredAssignment], now: datetime) -> Message:
    """Single summary for the first run, instead of one push per existing assignment."""
    shown = sorted(records, key=lambda r: r.due)[:10]
    lines = [f"• {r.label}: {format_local(r.due)}" for r in shown]
    if len(records) > len(shown):
        lines.append(f"…and {len(records) - len(shown)} more")
    title = f"Canvas reminders are on: tracking {len(records)} assignments"
    return Message(title, "\n".join(lines), ("white_check_mark",))


def _with_course(title: str, record: StoredAssignment) -> str:
    return f"{title} [{record.course}]" if record.course else title


def digest_message(
    records: Sequence[StoredAssignment], now: datetime, days: int, period: str
) -> Message | None:
    """'Due this week' summary: everything still active and due within `days`, or None."""
    horizon = now + timedelta(days=days)
    upcoming = sorted(
        (r for r in records if r.status is Status.ACTIVE and now < r.due <= horizon),
        key=lambda r: r.due,
    )
    if not upcoming:
        return None
    lines = [f"• {r.label}: {format_local(r.due)} (in {humanize(r.due - now)})" for r in upcoming]
    title = f"Your {period} Canvas summary: {len(upcoming)} due in the next {days} days"
    return Message(title, "\n".join(lines), ("spiral_calendar",))

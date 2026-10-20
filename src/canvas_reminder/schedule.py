"""Time maths: local timezone, which reminders are due, and human-readable durations. Pure."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Australia/Melbourne")


def due_leads(due: datetime, leads: Sequence[timedelta], now: datetime) -> list[timedelta]:
    """Lead times whose reminder moment has passed while the assignment is still upcoming.

    With leads [2d, 2h] and an assignment due in 30 hours, this returns [2d].
    """
    if now >= due:
        return []
    return [lead for lead in leads if due - lead <= now]


def next_lead(due: datetime, leads: Sequence[timedelta], now: datetime) -> timedelta:
    """The lead time whose reminder fires next, for apps that allow only one alert.

    With leads [2d, 2h]: the 2d alert until it fires, then the 2h one. Once all
    have fired, the last one stays.
    """
    upcoming = [lead for lead in leads if due - lead > now]
    return max(upcoming) if upcoming else min(leads)


def format_local(moment: datetime) -> str:
    """'Sat 24 Oct, 11:59 pm AEDT' in Melbourne time, daylight saving included."""
    local = moment.astimezone(LOCAL_TZ)
    hour = local.hour % 12 or 12
    meridiem = "am" if local.hour < 12 else "pm"
    return f"{local:%a} {local.day} {local:%b}, {hour}:{local:%M} {meridiem} {local:%Z}"


def humanize(delta: timedelta) -> str:
    """'2 days 3 hours', '1 hour 5 minutes', 'less than a minute'."""
    minutes = max(int(delta.total_seconds() // 60), 0)
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = [(days, "day"), (hours, "hour")] if days else [(hours, "hour"), (mins, "minute")]
    text = " ".join(f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in parts if n)
    return text or "less than a minute"

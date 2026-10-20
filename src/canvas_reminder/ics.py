"""Fetch the Canvas calendar feed and parse assignments out of it.

Parsing is a pure function of the ICS text, so it is tested against a committed,
anonymised fixture rather than a real feed.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time

import requests
from icalendar import Calendar

from .http_client import HttpError, request
from .models import Assignment
from .schedule import LOCAL_TZ

# Canvas assignment events: UID "event-assignment-123", SUMMARY "Title [COURSE_CODE]".
_ASSIGNMENT_UID = re.compile(r"^event-assignment-\d+$")
_SUMMARY = re.compile(r"^(?P<title>.*?)\s*\[(?P<course>[^\[\]]+)\]$")
# Canvas exports all-day due dates as DATE values; treat them as due at end of day.
DATE_ONLY_DUE_TIME = time(23, 59)


class FeedError(Exception):
    """The feed could not be fetched or parsed. Messages never include the feed URL."""


def normalize_feed_url(value: str) -> str:
    """Accept the link as copied from Canvas (https://, or webcal:// from calendar apps)."""
    url = value.strip()
    if url.startswith("webcal://"):
        url = "https://" + url.removeprefix("webcal://")
    if not url.startswith("https://"):
        raise FeedError(
            "The saved Canvas feed link is not a web address (it should start with https://). "
            "Copy it again from Canvas > Calendar > Calendar Feed and update the "
            "canvas-reminder/ics-url secret."
        )
    return url


def fetch_ics(session: requests.Session, url: str) -> str:
    url = normalize_feed_url(url)
    try:
        return request(session, "GET", url).text
    except HttpError as exc:
        raise FeedError(f"Could not fetch the Canvas feed: {exc}") from None


def parse_assignments(ics_text: str) -> list[Assignment]:
    """Return the assignments in an ICS document, sorted by due date."""
    try:
        calendar = Calendar.from_ical(ics_text)
    except (ValueError, IndexError):
        raise FeedError("The Canvas feed is not a valid ICS calendar") from None

    assignments = {}
    for event in calendar.walk("VEVENT"):
        uid = str(event.get("UID", ""))
        if not _ASSIGNMENT_UID.match(uid) or "DTSTART" not in event:
            continue
        title, course = split_summary(str(event.get("SUMMARY", "")))
        assignments[uid] = Assignment(
            uid=uid,
            title=title,
            course=course,
            due=to_utc(event.decoded("DTSTART")),
            description=" ".join(str(event.get("DESCRIPTION", "")).split()),
            url=str(event["URL"]) if "URL" in event else None,
        )
    return sorted(assignments.values(), key=lambda a: (a.due, a.uid))


def split_summary(summary: str) -> tuple[str, str]:
    """'Essay 1 [ABC10001_2026_SM2]' -> ('Essay 1', 'ABC10001_2026_SM2')."""
    match = _SUMMARY.match(summary.strip())
    if match is None:
        return summary.strip(), ""
    return match["title"], match["course"]


def to_utc(value: date | datetime) -> datetime:
    """Normalise an ICS date/datetime to aware UTC.

    UTC and TZID times convert directly. Floating times and all-day dates are taken
    as Melbourne local time, so daylight saving is applied for that specific date.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=LOCAL_TZ)
        return value.astimezone(UTC)
    return datetime.combine(value, DATE_ONLY_DUE_TIME, tzinfo=LOCAL_TZ).astimezone(UTC)

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from canvas_reminder.messages import (
    bootstrap_message,
    due_soon_message,
    event_message,
    removed_message,
)
from canvas_reminder.schedule import due_leads, format_local, humanize
from tests.conftest import NOW, make_assignment, stored

LEADS = (timedelta(days=2), timedelta(hours=2))
DUE = datetime(2026, 10, 24, 12, 59, tzinfo=UTC)


@pytest.mark.parametrize(
    ("before_due", "expected"),
    [
        (timedelta(days=5), []),
        (timedelta(days=2), [timedelta(days=2)]),  # exactly at the reminder moment
        (timedelta(days=1), [timedelta(days=2)]),
        (timedelta(hours=1), list(LEADS)),
        (timedelta(0), []),  # due now: too late
        (-timedelta(hours=1), []),
    ],
)
def test_due_leads(before_due: timedelta, expected: list) -> None:
    assert due_leads(DUE, LEADS, DUE - before_due) == expected


def test_format_local_handles_daylight_saving() -> None:
    assert format_local(DUE) == "Sat 24 Oct, 11:59 pm AEDT"
    assert format_local(datetime(2026, 6, 1, 0, 5, tzinfo=UTC)) == "Mon 1 Jun, 10:05 am AEST"


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(days=2, hours=3, minutes=10), "2 days 3 hours"),
        (timedelta(days=1), "1 day"),
        (timedelta(hours=1, minutes=5), "1 hour 5 minutes"),
        (timedelta(minutes=1), "1 minute"),
        (timedelta(seconds=30), "less than a minute"),
        (-timedelta(hours=1), "less than a minute"),
    ],
)
def test_humanize(delta: timedelta, text: str) -> None:
    assert humanize(delta) == text


def test_event_messages() -> None:
    record = stored(make_assignment())
    new = event_message(record, NOW)
    assert new.title == "New: Essay [ABC10001_2026_SM2]"
    assert "Sat 24 Oct, 11:59 pm AEDT" in new.body and new.click == record.url

    moved = replace(record, version=2, previous_due=DUE - timedelta(days=1))
    msg = event_message(moved, NOW)
    assert msg.title.startswith("Due date changed") and "Was Fri 23 Oct" in msg.body

    assert event_message(replace(record, version=3), NOW).title.startswith("Updated")


def test_due_soon_removed_and_bootstrap_messages() -> None:
    record = stored(make_assignment(course=""))
    soon = due_soon_message(record, DUE - timedelta(hours=2))
    assert soon.title == "Due in 2 hours: Essay" and soon.priority == 4
    assert removed_message(record).title == "Removed: Essay"

    many = [stored(make_assignment(f"u{i}", due=DUE + timedelta(days=i))) for i in range(12)]
    summary = bootstrap_message(many, NOW)
    assert "tracking 12 assignments" in summary.title
    assert summary.body.count("•") == 10 and "and 2 more" in summary.body


@pytest.mark.parametrize(
    ("before_due", "expected"),
    [
        (timedelta(days=5), timedelta(days=2)),
        (timedelta(days=1), timedelta(hours=2)),  # 2d alert already fired
        (timedelta(hours=1), timedelta(hours=2)),  # all fired: keep the last
    ],
)
def test_next_lead(before_due: timedelta, expected: timedelta) -> None:
    from canvas_reminder.schedule import next_lead

    assert next_lead(DUE, LEADS, DUE - before_due) == expected

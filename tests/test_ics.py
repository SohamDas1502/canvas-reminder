from datetime import UTC, date, datetime

import pytest
import requests
import responses

from canvas_reminder.http_client import make_session
from canvas_reminder.ics import FeedError, fetch_ics, parse_assignments, split_summary, to_utc
from tests.conftest import sample_ics

FAKE_FEED = "https://canvas.example.edu/feeds/calendars/user_FAKEtoken1234567890abcdef.ics"


def by_uid() -> dict:
    return {a.uid: a for a in parse_assignments(sample_ics())}


def test_only_assignment_events_are_kept() -> None:
    uids = set(by_uid())
    assert "event-calendar-event-555" not in uids
    assert uids == {f"event-assignment-10000{i}" for i in range(1, 7)}


def test_title_and_course_are_split_from_summary() -> None:
    a = by_uid()["event-assignment-100001"]
    assert (a.title, a.course) == ("Assignment 1: Systems Report", "DEMO10001_2026_SM2")
    assert a.url == "https://canvas.example.edu/courses/1001/assignments/100001"
    assert a.description.startswith("Write a 2000-word report")


def test_summary_without_course_keeps_whole_title() -> None:
    assert by_uid()["event-assignment-100006"].course == ""
    assert split_summary("Quiz [A] extra") == ("Quiz [A] extra", "")
    assert split_summary("  Quiz 2 [XYZ] ") == ("Quiz 2", "XYZ")


def test_utc_times_are_kept() -> None:
    assert by_uid()["event-assignment-100001"].due == datetime(2026, 10, 24, 12, 59, tzinfo=UTC)


def test_date_only_due_is_end_of_day_in_melbourne() -> None:
    # 3 Nov 2026 is during daylight saving (AEDT, UTC+11): 23:59 local = 12:59 UTC.
    assert by_uid()["event-assignment-100002"].due == datetime(2026, 11, 3, 12, 59, tzinfo=UTC)


def test_tzid_times_are_converted() -> None:
    assert by_uid()["event-assignment-100003"].due == datetime(2026, 11, 2, 6, 0, tzinfo=UTC)


def test_results_are_sorted_by_due_date() -> None:
    dues = [a.due for a in parse_assignments(sample_ics())]
    assert dues == sorted(dues)


@pytest.mark.parametrize(
    ("day", "expected_utc_hour"),
    [
        (date(2026, 4, 4), 12),  # last day of AEDT (UTC+11)
        (date(2026, 4, 5), 13),  # daylight saving ended at 3am: AEST (UTC+10)
        (date(2026, 10, 3), 13),  # last day of AEST
        (date(2026, 10, 4), 12),  # daylight saving started at 2am: AEDT
    ],
)
def test_daylight_saving_boundaries(day: date, expected_utc_hour: int) -> None:
    due = to_utc(day)
    assert (due.date(), due.hour, due.minute) == (day, expected_utc_hour, 59)


def test_floating_time_is_melbourne_local() -> None:
    assert to_utc(datetime(2026, 7, 1, 9, 0)) == datetime(2026, 6, 30, 23, 0, tzinfo=UTC)


def test_invalid_ics_raises_feed_error() -> None:
    with pytest.raises(FeedError):
        parse_assignments("this is not a calendar")


@responses.activate
def test_fetch_returns_body() -> None:
    responses.get(FAKE_FEED, body=sample_ics())
    assert "event-assignment-100001" in fetch_ics(make_session(retries=0), FAKE_FEED)


@responses.activate
@pytest.mark.parametrize("failure", [404, requests.ConnectionError("boom " + FAKE_FEED)])
def test_fetch_errors_never_contain_the_url(failure: object) -> None:
    if isinstance(failure, int):
        responses.get(FAKE_FEED, status=failure)
    else:
        responses.get(FAKE_FEED, body=failure)  # type: ignore[arg-type]
    with pytest.raises(FeedError) as excinfo:
        fetch_ics(make_session(retries=0), FAKE_FEED)
    assert "user_FAKE" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


@responses.activate
def test_webcal_links_are_accepted() -> None:
    responses.get(FAKE_FEED, body=sample_ics())
    webcal = FAKE_FEED.replace("https://", "webcal://")
    assert "event-assignment-100001" in fetch_ics(make_session(retries=0), f"  {webcal}\n")


@pytest.mark.parametrize(
    "value", ["canvas-x_notaurlatall_1234567", "", "http://insecure.example/x.ics"]
)
def test_non_url_secret_gets_a_clear_error(value: str) -> None:
    with pytest.raises(FeedError, match="not a web address"):
        fetch_ics(make_session(retries=0), value)

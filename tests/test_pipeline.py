"""End-to-end pipeline behaviour against moto DynamoDB and in-memory channels.

These are the "definition of done" checks: no duplicates across runs, retries or races;
a due-date change updates the existing reminder; removed assignments are handled.
"""

from datetime import UTC, datetime, timedelta

import pytest
import requests

from canvas_reminder.config import Config
from canvas_reminder.http_client import HttpError
from canvas_reminder.ics import FeedError
from canvas_reminder.models import Status
from canvas_reminder.pipeline import Deps, PipelineError, Reconciler, run
from canvas_reminder.state import StateRepository
from tests.conftest import NOW, sample_ics
from tests.fakes import FakeCalendar, FakePush

ESSAY = "event-assignment-100001"  # due Sat 24 Oct 2026 23:59 AEDT
CONFIG = Config(channels=("ntfy", "gcal"), ignore_courses=("SKIP",))


class World:
    """A feed we can edit, the real state layer, and fake channels."""

    def __init__(self, repo: StateRepository, config: Config = CONFIG) -> None:
        self.feed = sample_ics()
        self.push = FakePush()
        self.calendar = FakeCalendar()
        self.repo = repo
        self.deps = Deps(config, repo, [self.push, self.calendar], lambda: self.feed)

    def run(self, now: datetime = NOW) -> dict[str, int]:
        return run(self.deps, now)

    def use(self, fixture: str) -> None:
        self.feed = sample_ics(fixture)

    def drop(self, uid: str) -> None:
        start = self.feed.index(f"UID:{uid}")
        begin = self.feed.rindex("BEGIN:VEVENT", 0, start)
        end = self.feed.index("END:VEVENT", start) + len("END:VEVENT\n")
        self.feed = self.feed[:begin] + self.feed[end:]


@pytest.fixture
def world(repo: StateRepository) -> World:
    return World(repo)


def test_first_run_sends_one_summary_and_creates_one_event_each(world: World) -> None:
    summary = world.run()
    # 6 assignments in the feed: 1 in the past, 1 ignored course -> 4 tracked.
    assert summary["assignments"] == 4 and summary["new"] == 4
    assert world.push.titles == ["Canvas reminders are on: tracking 4 assignments"]
    assert world.calendar.creates == 4 and world.calendar.updates == 0


def test_running_twice_sends_nothing_new(world: World) -> None:
    world.run()
    pushes, creates = len(world.push.sent), world.calendar.creates
    summary = world.run(NOW + timedelta(minutes=30))
    assert summary["unchanged"] == 4
    assert len(world.push.sent) == pushes
    assert (world.calendar.creates, world.calendar.updates) == (creates, 0)


def test_due_date_change_updates_existing_reminder(world: World) -> None:
    world.run()
    world.use("sample_due_changed.ics")
    summary = world.run(NOW + timedelta(minutes=30))

    assert summary["changed"] == 1
    assert (
        world.push.titles[-1]
        == "Due date changed: Assignment 1: Systems Report [DEMO10001_2026_SM2]"
    )
    assert "Was Sat 24 Oct, 11:59 pm AEDT" in world.push.sent[-1].body
    assert (world.calendar.creates, world.calendar.updates) == (4, 1)  # updated, not added
    record = world.repo.get(ESSAY)
    assert record is not None
    event = world.calendar.events[record.refs["gcal"]["ref"]]
    assert event["due"] == datetime(2026, 10, 27, 12, 59, tzinfo=UTC)

    world.run(NOW + timedelta(hours=1))  # and only once
    assert world.calendar.updates == 1 and len(world.push.sent) == 2


def test_new_assignment_after_first_run_is_announced(world: World) -> None:
    world.drop("event-assignment-100003")
    world.run()
    world.use("sample.ics")
    world.run(NOW + timedelta(minutes=30))
    assert world.push.titles[-1] == "New: Lab 4 Quiz [DEMO20002_2026_SM2]"


def test_due_soon_reminders_fire_once_per_lead(world: World) -> None:
    world.run()
    two_days_before = datetime(2026, 10, 22, 13, 0, tzinfo=UTC)
    world.run(two_days_before)
    world.run(two_days_before + timedelta(minutes=30))
    assert world.push.titles[1:] == [
        "Due in 1 day 23 hours: Assignment 1: Systems Report [DEMO10001_2026_SM2]"
    ]

    two_hours_before = datetime(2026, 10, 24, 11, 0, tzinfo=UTC)
    world.run(two_hours_before)
    world.run(two_hours_before + timedelta(minutes=30))
    assert len(world.push.sent) == 3 and world.push.titles[2].startswith("Due in 1 hour 59 minutes")


def test_missed_runs_send_one_reminder_not_a_burst(world: World) -> None:
    world.run()
    world.run(datetime(2026, 10, 24, 12, 0, tzinfo=UTC))  # both 2d and 2h already passed
    assert len(world.push.sent) == 2
    world.run(datetime(2026, 10, 24, 12, 30, tzinfo=UTC))
    assert len(world.push.sent) == 2


def test_new_assignment_already_inside_lead_window_gets_one_push(repo: StateRepository) -> None:
    world = World(repo)
    world.drop(ESSAY)
    world.run()
    world.use("sample.ics")
    world.run(datetime(2026, 10, 23, 0, 0, tzinfo=UTC))  # due in ~37h, 2d lead passed
    assert world.push.titles[1:] == ["New: Assignment 1: Systems Report [DEMO10001_2026_SM2]"]
    world.run(datetime(2026, 10, 23, 0, 30, tzinfo=UTC))
    assert len(world.push.sent) == 2


def test_removed_assignment_notifies_once_and_keeps_reminder_by_default(world: World) -> None:
    world.run()
    world.drop(ESSAY)
    world.run(NOW + timedelta(minutes=30))
    world.run(NOW + timedelta(hours=1))
    assert world.push.titles[1:] == ["Removed: Assignment 1: Systems Report [DEMO10001_2026_SM2]"]
    assert world.calendar.deleted == []
    record = world.repo.get(ESSAY)
    assert record is not None and record.status is Status.REMOVED


def test_removed_assignment_reminder_deleted_when_configured(repo: StateRepository) -> None:
    world = World(repo, Config(channels=("ntfy", "gcal"), on_removed="delete"))
    world.run()
    world.drop(ESSAY)
    world.run(NOW + timedelta(minutes=30))
    assert len(world.calendar.deleted) == 1
    record = world.repo.get(ESSAY)
    assert record is not None and "gcal" not in record.refs

    world.use("sample.ics")  # it comes back: recreated and announced
    world.run(NOW + timedelta(hours=1))
    assert world.push.titles[-1].startswith("Updated: Assignment 1")
    assert world.calendar.creates == 6  # 5 originals (nothing ignored here) + 1 recreated


def test_removed_ignore_mode_is_silent(repo: StateRepository) -> None:
    world = World(repo, Config(channels=("ntfy",), on_removed="ignore"))
    world.run()
    world.drop(ESSAY)
    world.run(NOW + timedelta(minutes=30))
    assert len(world.push.sent) == 1


def test_empty_feed_aborts_instead_of_removing_everything(world: World) -> None:
    world.run()
    world.feed = "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"
    with pytest.raises(FeedError):
        world.run(NOW + timedelta(minutes=30))
    assert all(r.status is Status.ACTIVE for r in world.repo.load_all().values())
    assert len(world.push.sent) == 1


def test_refused_push_is_released_and_retried_next_run(world: World) -> None:
    world.run()
    world.use("sample_due_changed.ics")
    world.push.fail_with = HttpError("POST ntfy.sh returned HTTP 503", 503)
    with pytest.raises(PipelineError):
        world.run(NOW + timedelta(minutes=30))
    assert world.calendar.updates == 1  # other channels still progressed

    world.push.fail_with = None
    world.run(NOW + timedelta(hours=1))
    world.run(NOW + timedelta(hours=2))
    assert (
        world.push.titles.count(
            "Due date changed: Assignment 1: Systems Report [DEMO10001_2026_SM2]"
        )
        == 1
    )


def test_ambiguous_push_failure_is_not_retried(world: World) -> None:
    world.run()
    world.use("sample_due_changed.ics")
    world.push.fail_with = requests.ReadTimeout()  # may have been delivered
    with pytest.raises(PipelineError):
        world.run(NOW + timedelta(minutes=30))
    world.push.fail_with = None
    world.run(NOW + timedelta(hours=1))
    assert len(world.push.sent) == 1  # only the bootstrap; no possible duplicate


def test_failed_calendar_sync_is_retried_without_duplicates(world: World) -> None:
    world.calendar.fail = True
    with pytest.raises(PipelineError):
        world.run()
    world.calendar.fail = False
    world.run(NOW + timedelta(minutes=30))
    world.run(NOW + timedelta(hours=1))
    assert world.calendar.creates == 4 and world.calendar.updates == 0


def test_concurrent_runs_on_stale_state_cannot_double_send(world: World) -> None:
    """Two runs that read the same snapshot: only one wins each claim and lease."""
    world.run()
    world.use("sample_due_changed.ics")
    world.run(NOW + timedelta(minutes=30))  # records the change, sends once
    stale = world.repo.get(ESSAY)
    assert stale is not None

    # Pretend a second run had loaded this record before the first one finished.
    unsynced = type(stale)(**{**stale.__dict__, "sent": frozenset(), "refs": {}})
    Reconciler(world.deps, NOW + timedelta(minutes=30)).reconcile(unsynced)
    assert len(world.push.sent) == 2 and world.calendar.creates == 4


def test_calendar_held_lease_is_skipped(world: World) -> None:
    world.run()
    world.use("sample_due_changed.ics")
    later = NOW + timedelta(minutes=30)
    world.repo.acquire_lease(ESSAY, "gcal", later)  # someone else is mid-sync
    world.run(later)
    assert world.calendar.updates == 0
    world.run(later + timedelta(minutes=10))  # lease expired
    assert world.calendar.updates == 1


def test_per_course_lead_times_reach_the_calendar(repo: StateRepository) -> None:
    config = Config(
        channels=("gcal",),
        course_lead_times={"DEMO2": (timedelta(days=5), timedelta(hours=1))},
    )
    world = World(repo, config)
    world.run()
    leads = {e["uid"]: e["leads"] for e in world.calendar.events.values()}
    assert leads["event-assignment-100002"] == (timedelta(days=5), timedelta(hours=1))
    assert leads[ESSAY] == (timedelta(days=2), timedelta(hours=2))


def test_changing_lead_times_updates_existing_reminders_in_place(repo: StateRepository) -> None:
    world = World(repo)
    world.run()
    world.deps.config = Config(
        channels=("ntfy", "gcal"), ignore_courses=("SKIP",), lead_times=(timedelta(days=3),)
    )
    world.run(NOW + timedelta(minutes=30))
    assert world.calendar.creates == 4 and world.calendar.updates == 4
    assert {e["leads"] for e in world.calendar.events.values()} == {(timedelta(days=3),)}
    world.run(NOW + timedelta(hours=1))
    assert world.calendar.updates == 4  # and only once


def test_single_alert_channel_moves_to_the_next_lead(repo: StateRepository) -> None:
    from tests.fakes import FakeTodo

    world = World(repo)
    todo = FakeTodo()
    world.deps.notifiers = [world.push, todo]
    world.run()
    [essay] = [e for e in todo.events.values() if e["uid"] == ESSAY]
    assert essay["leads"] == (timedelta(days=2),)

    world.run(datetime(2026, 10, 22, 13, 0, tzinfo=UTC))  # the 2-day alert has fired
    [essay] = [e for e in todo.events.values() if e["uid"] == ESSAY]
    assert essay["leads"] == (timedelta(hours=2),)
    updates = todo.updates
    world.run(datetime(2026, 10, 22, 13, 30, tzinfo=UTC))
    assert todo.updates == updates  # nothing more until something changes


def test_first_run_summary_comes_before_urgent_reminders(world: World) -> None:
    world.run(datetime(2026, 10, 23, 0, 0, tzinfo=UTC))  # Assignment 1 already inside 2 days
    assert world.push.titles[0].startswith("Canvas reminders are on")
    assert world.push.titles[1].startswith("Due in 1 day")

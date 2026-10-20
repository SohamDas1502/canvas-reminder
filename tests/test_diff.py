from dataclasses import replace
from datetime import timedelta

from canvas_reminder.diff import ChangeKind, diff_assignments, looks_truncated
from canvas_reminder.models import Status
from tests.conftest import NOW, make_assignment, stored


def kinds(changes: list) -> dict[str, ChangeKind]:
    return {c.uid: c.kind for c in changes}


def test_unseen_assignment_is_new() -> None:
    a = make_assignment()
    [change] = diff_assignments([a], {}, NOW)
    assert change.kind is ChangeKind.NEW and change.current == a and change.previous is None


def test_identical_assignment_is_unchanged() -> None:
    a = make_assignment()
    assert kinds(diff_assignments([a], {a.uid: stored(a)}, NOW)) == {a.uid: ChangeKind.UNCHANGED}


def test_due_title_or_description_change_is_detected() -> None:
    a = make_assignment()
    previous = {a.uid: stored(a)}
    for edited in (
        replace(a, due=a.due + timedelta(days=1)),
        replace(a, title="Essay (revised)"),
        replace(a, description="New instructions"),
    ):
        assert kinds(diff_assignments([edited], previous, NOW)) == {a.uid: ChangeKind.CHANGED}


def test_course_or_url_change_alone_is_not_a_change() -> None:
    a = make_assignment()
    moved = replace(a, url="https://canvas.example.edu/other")
    assert kinds(diff_assignments([moved], {a.uid: stored(a)}, NOW))[a.uid] is ChangeKind.UNCHANGED


def test_missing_upcoming_assignment_is_removed() -> None:
    a = make_assignment()
    [change] = diff_assignments([], {a.uid: stored(a)}, NOW)
    assert change.kind is ChangeKind.REMOVED and change.current is None


def test_missing_past_assignment_just_aged_out() -> None:
    a = make_assignment(due=NOW - timedelta(hours=1))
    assert diff_assignments([], {a.uid: stored(a)}, NOW) == []


def test_already_removed_is_not_removed_again() -> None:
    a = make_assignment()
    assert diff_assignments([], {a.uid: stored(a, status=Status.REMOVED)}, NOW) == []


def test_reappearing_assignment_counts_as_changed() -> None:
    a = make_assignment()
    previous = {a.uid: stored(a, status=Status.REMOVED)}
    assert kinds(diff_assignments([a], previous, NOW)) == {a.uid: ChangeKind.CHANGED}


def test_mixed_batch() -> None:
    same, edited, gone, fresh = (make_assignment(f"event-assignment-{i}") for i in range(4))
    previous = {a.uid: stored(a) for a in (same, edited, gone)}
    current = [same, replace(edited, title="Edited"), fresh]
    assert kinds(diff_assignments(current, previous, NOW)) == {
        same.uid: ChangeKind.UNCHANGED,
        edited.uid: ChangeKind.CHANGED,
        gone.uid: ChangeKind.REMOVED,
        fresh.uid: ChangeKind.NEW,
    }


def test_empty_feed_with_upcoming_records_looks_truncated() -> None:
    a = make_assignment()
    assert looks_truncated([], {a.uid: stored(a)}, NOW)
    assert not looks_truncated([a], {a.uid: stored(a)}, NOW)
    assert not looks_truncated([], {}, NOW)
    past = make_assignment(due=NOW - timedelta(days=1))
    assert not looks_truncated([], {past.uid: stored(past)}, NOW)

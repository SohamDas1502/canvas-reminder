from dataclasses import replace
from datetime import timedelta

import pytest
from botocore.exceptions import ClientError

from canvas_reminder.models import Status
from canvas_reminder.state import LEASE_DURATION, StateRepository
from tests.conftest import NOW, make_assignment


def test_create_is_insert_only(repo: StateRepository) -> None:
    a = make_assignment()
    assert repo.create(a, NOW)
    assert not repo.create(replace(a, title="Other"), NOW)  # concurrent/retried run
    record = repo.get(a.uid)
    assert record is not None
    assert (record.title, record.version, record.status) == ("Essay", 1, Status.ACTIVE)
    assert record.due == a.due and record.content_hash == a.content_hash
    assert record.refs == {} and record.sent == frozenset()


def test_ttl_is_set_after_due_date(repo: StateRepository, table) -> None:  # type: ignore[no-untyped-def]
    a = make_assignment()
    repo.create(a, NOW)
    ttl = int(table.get_item(Key={"uid": a.uid})["Item"]["ttl"])
    assert ttl == int((a.due + timedelta(days=30)).timestamp())


def test_create_can_preclaim_keys(repo: StateRepository) -> None:
    a = make_assignment()
    repo.create(a, NOW, sent={"ntfy:event#v1"})
    assert not repo.claim(a.uid, ["ntfy:event#v1"])


def test_apply_change_uses_optimistic_locking(repo: StateRepository) -> None:
    a = make_assignment()
    repo.create(a, NOW)
    moved = replace(a, due=a.due + timedelta(days=3))
    assert repo.apply_change(moved, expected_version=1, previous_due=a.due, now=NOW)
    assert not repo.apply_change(moved, expected_version=1, previous_due=a.due, now=NOW)
    record = repo.get(a.uid)
    assert record is not None
    assert (record.version, record.due, record.previous_due) == (2, moved.due, a.due)
    assert record.content_hash == moved.content_hash


def test_mark_removed_then_reinstate(repo: StateRepository) -> None:
    a = make_assignment()
    repo.create(a, NOW)
    assert repo.mark_removed(a.uid, expected_version=1, now=NOW)
    assert not repo.mark_removed(a.uid, expected_version=1, now=NOW)
    record = repo.get(a.uid)
    assert record is not None and (record.status, record.version) == (Status.REMOVED, 2)
    assert repo.apply_change(a, expected_version=2, previous_due=a.due, now=NOW)
    record = repo.get(a.uid)
    assert record is not None and record.status is Status.ACTIVE


def test_claim_is_exactly_once(repo: StateRepository) -> None:
    a = make_assignment()
    repo.create(a, NOW)
    assert repo.claim(a.uid, ["ntfy:event#v1", "ntfy:due#x#7200"])
    assert not repo.claim(a.uid, ["ntfy:event#v1"])
    assert not repo.claim(a.uid, ["ntfy:other", "ntfy:due#x#7200"])  # all-or-nothing
    assert repo.claim(a.uid, ["ntfy:other"])
    record = repo.get(a.uid)
    assert record is not None
    assert record.sent == {"ntfy:event#v1", "ntfy:due#x#7200", "ntfy:other"}


def test_release_allows_a_retry(repo: StateRepository) -> None:
    a = make_assignment()
    repo.create(a, NOW)
    repo.claim(a.uid, ["k"])
    repo.release(a.uid, ["k"])
    assert repo.claim(a.uid, ["k"])


def test_claim_on_missing_item_does_not_create_it(repo: StateRepository) -> None:
    assert not repo.claim("ghost", ["k"])
    assert repo.get("ghost") is None
    repo.touch("ghost", NOW)
    assert repo.get("ghost") is None


def test_lease_is_exclusive_until_it_expires(repo: StateRepository) -> None:
    a = make_assignment()
    repo.create(a, NOW)
    lease = repo.acquire_lease(a.uid, "gcal", NOW)
    assert lease is not None
    assert repo.acquire_lease(a.uid, "gcal", NOW + timedelta(minutes=1)) is None
    assert repo.acquire_lease(a.uid, "mstodo", NOW) is not None  # per channel
    later = NOW + LEASE_DURATION + timedelta(seconds=1)
    assert repo.acquire_lease(a.uid, "gcal", later) is not None  # crashed holder


def test_commit_ref_requires_the_lease(repo: StateRepository) -> None:
    a = make_assignment()
    repo.create(a, NOW)
    lease = repo.acquire_lease(a.uid, "gcal", NOW)
    assert lease is not None
    assert not repo.commit_ref(a.uid, "gcal", "evt1", a.content_hash, lease + 1)
    assert repo.commit_ref(a.uid, "gcal", "evt1", a.content_hash, lease)
    record = repo.get(a.uid)
    assert record is not None and record.refs == {"gcal": {"ref": "evt1", "hash": a.content_hash}}
    assert repo.acquire_lease(a.uid, "gcal", NOW) is not None  # commit released it

    repo.release_lease(a.uid, "gcal")
    repo.drop_ref(a.uid, "gcal")
    record = repo.get(a.uid)
    assert record is not None and record.refs == {}


def test_load_all_pages_through_the_table(repo: StateRepository, table, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for i in range(5):
        repo.create(make_assignment(f"event-assignment-{i}"), NOW)
    real_scan = table.scan
    monkeypatch.setattr(table, "scan", lambda **kw: real_scan(Limit=2, **kw))
    assert set(repo.load_all()) == {f"event-assignment-{i}" for i in range(5)}


def test_unexpected_errors_propagate(repo: StateRepository, table, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    error = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "op")

    def boom(**_: object) -> None:
        raise error

    monkeypatch.setattr(table, "update_item", boom)
    monkeypatch.setattr(table, "put_item", boom)
    with pytest.raises(ClientError):
        repo.claim("x", ["k"])
    with pytest.raises(ClientError):
        repo.create(make_assignment(), NOW)

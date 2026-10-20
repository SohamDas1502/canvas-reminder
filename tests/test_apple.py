import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import boto3
import pytest

from canvas_reminder import apple
from canvas_reminder.config import Config
from canvas_reminder.models import Status
from tests.conftest import NOW, make_assignment, stored

CONFIG = Config(channels=("apple",), ignore_courses=("SKIP",))
ESSAY = stored(make_assignment("event-assignment-1"))  # due Sat 24 Oct 23:59 AEDT


def test_feed_items_carry_due_alert_and_matching_tag() -> None:
    [item] = apple.build_feed([ESSAY], CONFIG, NOW)["items"]
    assert item["title"] == "Essay [ABC10001_2026_SM2]"
    assert item["due"] == "2026-10-24T23:59:00+11:00"
    assert item["alert"] == "2026-10-22T23:59:00+11:00"  # 2 days before
    assert item["alert_text"] == "Thu 22 Oct, 11:59 pm AEDT"
    assert item["notes"].endswith("canvas-id: event-assignment-1")
    assert item["action"] == "add-or-update"


def test_alert_moves_to_the_next_lead_after_one_fires() -> None:
    later = datetime(2026, 10, 23, 0, 0, tzinfo=UTC)
    [item] = apple.build_feed([ESSAY], CONFIG, later)["items"]
    assert item["alert"] == "2026-10-24T21:59:00+11:00"  # 2 hours before


def test_feed_skips_past_and_ignored_and_honours_on_removed() -> None:
    past = replace(ESSAY, uid="p", due=NOW - timedelta(hours=1))
    ignored = replace(ESSAY, uid="i", course="SKIP30003")
    removed = replace(ESSAY, uid="r", status=Status.REMOVED)
    feed = apple.build_feed([past, ignored, removed], CONFIG, NOW)
    assert [i["id"] for i in feed["items"]] == ["r"]
    assert feed["items"][0]["action"] == "add-or-update"
    delete_mode = Config(channels=("apple",), on_removed="delete")
    assert apple.build_feed([removed], delete_mode, NOW)["items"][0]["action"] == "delete"


@pytest.fixture
def feed_env(repo, table, monkeypatch):  # type: ignore[no-untyped-def]
    boto3.client("ssm").put_parameter(
        Name="/canvas-reminder/apple",
        Value='{"token": "correct-horse-battery"}',
        Type="SecureString",
    )
    repo.create(make_assignment("event-assignment-1", due=datetime(2099, 1, 1, tzinfo=UTC)), NOW)
    monkeypatch.setenv("TABLE_NAME", table.name)
    monkeypatch.setattr(apple, "_cached", None)
    monkeypatch.setattr(apple, "load_config", lambda: CONFIG)


def call(token: str | None) -> dict:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return apple.lambda_handler({"headers": headers}, None)


def test_handler_requires_the_token(feed_env) -> None:  # type: ignore[no-untyped-def]
    assert call(None)["statusCode"] == 401
    assert call("wrong")["statusCode"] == 401
    ok = call("correct-horse-battery")
    assert ok["statusCode"] == 200 and ok["headers"]["cache-control"] == "no-store"
    assert [i["id"] for i in json.loads(ok["body"])["items"]] == ["event-assignment-1"]


def test_handler_is_404_unless_enabled(feed_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(apple, "load_config", lambda: Config(channels=("ntfy",)))
    assert call("correct-horse-battery")["statusCode"] == 404

from datetime import timedelta

import pytest

from canvas_reminder.config import ConfigError
from canvas_reminder.web.worker import WebContext
from tests.conftest import NOW, make_assignment

FEED = "https://canvas.example.edu/feeds/calendars/user_FAKEtoken1234567890abcdef.ics"


def test_first_sign_in_creates_defaults_once(web: WebContext) -> None:
    first = web.users.ensure("u1", "a@example.edu", NOW)
    again = web.users.ensure("u1", "changed@example.edu", NOW)
    assert first == again
    assert first.channels == ("email",) and first.lead_times == ("2d", "2h")
    assert first.ntfy_topic.startswith("canvas-") and len(first.ntfy_topic) > 25
    assert not first.has_feed and first.status == "active"


def test_each_user_gets_their_own_ntfy_topic(web: WebContext) -> None:
    a = web.users.ensure("u1", "a@example.edu", NOW)
    b = web.users.ensure("u2", "b@example.edu", NOW)
    assert a.ntfy_topic != b.ntfy_topic


def test_feed_link_lives_in_its_own_encrypted_parameter(web: WebContext) -> None:
    import boto3

    web.users.ensure("u1", "a@example.edu", NOW)
    web.users.ensure("u2", "b@example.edu", NOW)
    web.users.set_feed("u1", FEED, NOW)

    raw = web.users._table.get_item(Key={"user_id": "u1"})["Item"]  # type: ignore[attr-defined]
    assert "FAKEtoken" not in str(raw) and raw["feed_set"] is True
    param = boto3.client("ssm").describe_parameters()["Parameters"][0]
    assert param["Name"] == "/canvas-reminder-web/feeds/u1" and param["Type"] == "SecureString"
    assert web.users.feed_url("u1") == FEED
    assert "feed_url" not in web.users.get("u1").public()  # type: ignore[union-attr]
    with pytest.raises(LookupError):
        web.users.feed_url("u2")  # each user only ever has their own
    with pytest.raises(LookupError):
        web.users.set_feed("nobody", FEED, NOW)


def test_preferences_are_validated(web: WebContext) -> None:
    web.users.ensure("u1", "a@example.edu", NOW)
    saved = web.users.update_preferences(
        "u1",
        channels=["ntfy", "email"],
        lead_times=["1d", "3d", "2h"],
        digest="weekly",
        paused=False,
        now=NOW,
    )
    assert saved.channels == ("email", "ntfy")  # stored in a fixed order
    assert saved.lead_times == ("3d", "1d", "2h") and saved.digest == "weekly"

    bad = [
        {"channels": [], "lead_times": ["1d"], "digest": "off"},
        {"channels": ["gcal"], "lead_times": ["1d"], "digest": "off"},
        {"channels": ["email"], "lead_times": ["1w"], "digest": "off"},
        {"channels": ["email"], "lead_times": [], "digest": "off"},
        {"channels": ["email"], "lead_times": ["1d"], "digest": "hourly"},
    ]
    for prefs in bad:
        with pytest.raises(ConfigError):
            web.users.update_preferences("u1", paused=False, now=NOW, **prefs)  # type: ignore[arg-type]

    paused = web.users.update_preferences(
        "u1", channels=["email"], lead_times=["1d"], digest="off", paused=True, now=NOW
    )
    assert paused.status == "paused"


def test_apple_token_is_hashed_verified_and_revocable(web: WebContext) -> None:
    web.users.ensure("u1", "a@example.edu", NOW)
    token = web.users.new_apple_token("u1")
    raw = web.users._table.get_item(Key={"user_id": "u1"})["Item"]  # type: ignore[attr-defined]
    assert token.split(".", 1)[1] not in str(raw)

    assert web.users.user_for_apple_token(token) == "u1"
    assert web.users.user_for_apple_token(token + "x") is None
    assert web.users.user_for_apple_token("u2." + token.split(".", 1)[1]) is None
    assert web.users.user_for_apple_token("garbage") is None

    newer = web.users.new_apple_token("u1")
    assert web.users.user_for_apple_token(token) is None  # old token revoked
    assert web.users.user_for_apple_token(newer) == "u1"


def test_only_active_users_with_a_feed_are_scheduled(web: WebContext) -> None:
    for uid in ("ready", "paused", "no-feed"):
        web.users.ensure(uid, f"{uid}@example.edu", NOW)
    web.users.set_feed("ready", FEED, NOW)
    web.users.set_feed("paused", FEED, NOW)
    web.users.update_preferences(
        "paused", channels=["email"], lead_times=["1d"], digest="off", paused=True, now=NOW
    )
    assert list(web.users.active_user_ids()) == ["ready"]


def test_record_run_and_claim_once(web: WebContext) -> None:
    web.users.ensure("u1", "a@example.edu", NOW)
    web.users.record_run("u1", NOW, "Could not fetch the Canvas feed: HTTP 404")
    assert web.users.get("u1").last_error.endswith("HTTP 404")  # type: ignore[union-attr]
    web.users.record_run("u1", NOW + timedelta(minutes=30), None)
    assert web.users.get("u1").last_error is None  # type: ignore[union-attr]
    web.users.record_run("ghost", NOW, None)  # never creates a user

    assert web.users.claim_once("u1", "last_digest", "daily:2026-10-20")
    assert not web.users.claim_once("u1", "last_digest", "daily:2026-10-20")
    assert web.users.claim_once("u1", "last_digest", "daily:2026-10-21")
    assert web.users.get("ghost") is None


def test_delete_removes_only_that_users_data(web: WebContext) -> None:
    for uid in ("u1", "u2"):
        web.users.ensure(uid, f"{uid}@example.edu", NOW)
        for i in range(3):
            web.repo(uid).create(make_assignment(f"event-assignment-{i}"), NOW)
    web.users.set_feed("u1", FEED, NOW)
    assert web.users.delete("u1", web.assignments) == 4
    with pytest.raises(LookupError):
        web.users.feed_url("u1")  # the feed parameter is gone too
    web.users.delete("u1", web.assignments)  # deleting twice is harmless
    assert web.users.get("u1") is None and web.repo("u1").load_all() == {}
    assert web.users.get("u2") is not None and len(web.repo("u2").load_all()) == 3

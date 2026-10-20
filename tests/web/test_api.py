import json
from typing import Any

import pytest
import responses

from canvas_reminder.web import api, worker
from canvas_reminder.web.worker import WebContext
from tests.conftest import NOW, sample_ics

FEED = "https://canvas.example.edu/feeds/calendars/user_FAKEtoken1234567890abcdef.ics"


@pytest.fixture
def call(web: WebContext, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(worker, "_context", web)
    monkeypatch.setattr(api, "context", lambda: web)
    monkeypatch.delenv("USER_POOL_ID", raising=False)

    class FrozenDatetime:
        @staticmethod
        def now(tz: object) -> object:
            return NOW

    monkeypatch.setattr(api, "datetime", FrozenDatetime)

    def _call(
        route: str,
        body: dict[str, Any] | None = None,
        user: str | None = "alice",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        claims = {"sub": user, "email": f"{user}@example.edu"} if user else {}
        event = {
            "routeKey": route,
            "body": json.dumps(body) if body is not None else None,
            "headers": headers or {},
            "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
        }
        response = api.lambda_handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    return _call


def test_get_me_creates_the_user_and_never_returns_the_feed(call, web) -> None:  # type: ignore[no-untyped-def]
    status, me = call("GET /api/me")
    assert status == 200 and me["email"] == "alice@example.edu"
    assert me["channels"] == ["email"] and me["feed_connected"] is False
    assert me["apple_feed_url"] == "https://app.example.edu/api/apple/feed"
    assert "feed" not in me


@responses.activate
def test_feed_link_is_checked_then_saved_with_a_preview(call, web) -> None:  # type: ignore[no-untyped-def]
    responses.get(FEED, body=sample_ics())
    status, body = call("PUT /api/me/feed", {"url": FEED})
    assert status == 200 and body["upcoming_count"] == 5
    assert body["preview"][0] == {
        "title": "Assignment 1: Systems Report",
        "course": "DEMO10001_2026_SM2",
        "due": "Sat 24 Oct, 11:59 pm AEDT",
    }
    assert web.users.feed_url("alice") == FEED


@responses.activate
@pytest.mark.parametrize("url", ["not a link", "http://insecure.example/x.ics", FEED])
def test_bad_feed_links_get_a_helpful_error(call, url) -> None:  # type: ignore[no-untyped-def]
    responses.get(FEED, status=404)
    status, body = call("PUT /api/me/feed", {"url": url})
    assert status == 400 and "Calendar Feed" in body["error"] and "FAKEtoken" not in body["error"]


def test_preferences_round_trip_and_validation(call) -> None:  # type: ignore[no-untyped-def]
    call("GET /api/me")
    prefs = {"channels": ["email", "ntfy"], "lead_times": ["1d", "2h"], "digest": "daily"}
    status, me = call("PUT /api/me/preferences", prefs)
    assert status == 200 and me["channels"] == ["email", "ntfy"] and me["digest"] == "daily"
    status, body = call("PUT /api/me/preferences", {**prefs, "lead_times": ["1w"]})
    assert status == 400 and "use e.g." in body["error"]


@responses.activate
def test_test_message_goes_to_each_channel_and_is_rate_limited(call, web) -> None:  # type: ignore[no-untyped-def]
    responses.post("https://ntfy.sh")
    call("GET /api/me")
    call("PUT /api/me/preferences", {"channels": ["email", "ntfy"], "lead_times": ["1d"]})
    status, body = call("POST /api/me/test")
    assert status == 200 and body["results"] == {"ntfy": "sent", "email": "sent"}
    assert web.ses.subjects == ["Test reminder from Canvas Reminders"]
    status, body = call("POST /api/me/test")
    assert status == 429


def test_test_message_reports_a_failed_channel(call, web) -> None:  # type: ignore[no-untyped-def]
    call("GET /api/me")
    web.ses.fail = True
    status, body = call("POST /api/me/test")
    assert status == 200 and body["results"] == {"email": "failed"}


@responses.activate
def test_apple_feed_needs_a_valid_token_and_the_channel_on(call, web) -> None:  # type: ignore[no-untyped-def]
    responses.get(FEED, body=sample_ics())
    call("PUT /api/me/feed", {"url": FEED})
    worker.run_for_user(web, "alice", NOW)
    _, body = call("POST /api/me/apple-token")
    auth = {"Authorization": f"Bearer {body['token']}"}

    assert call("GET /api/apple/feed", user=None)[0] == 401
    assert call("GET /api/apple/feed", user=None, headers={"Authorization": "Bearer x.y"})[0] == 401
    assert call("GET /api/apple/feed", user=None, headers=auth)[0] == 404  # apple not chosen

    call("PUT /api/me/preferences", {"channels": ["apple"], "lead_times": ["2d"]})
    status, feed = call("GET /api/apple/feed", user=None, headers=auth)
    assert status == 200 and len(feed["items"]) == 5


@responses.activate
def test_assignments_and_delete(call, web) -> None:  # type: ignore[no-untyped-def]
    responses.get(FEED, body=sample_ics())
    call("PUT /api/me/feed", {"url": FEED})
    worker.run_for_user(web, "alice", NOW)
    status, body = call("GET /api/me/assignments")
    assert status == 200 and len(body["assignments"]) == 5
    assert body["assignments"][0]["due"] == "Sat 24 Oct, 11:59 pm AEDT"

    status, body = call("DELETE /api/me")
    assert status == 200 and body["deleted_items"] == 6
    assert web.users.get("alice") is None


def test_unknown_routes_missing_sign_in_and_bad_bodies(call) -> None:  # type: ignore[no-untyped-def]
    assert call("GET /api/nope")[0] == 404
    assert call("GET /api/me", user=None)[0] == 401
    event = {"routeKey": "PUT /api/me/preferences", "body": "{not json", "requestContext": {}}
    assert api.lambda_handler(event, None)["statusCode"] == 400


def test_unexpected_errors_are_generic(call, web, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def boom(*_: object) -> None:
        raise RuntimeError("internal detail that must not leak")

    monkeypatch.setattr(web.users, "ensure", boom)
    status, body = call("GET /api/me")
    assert status == 500 and "internal detail" not in body["error"]

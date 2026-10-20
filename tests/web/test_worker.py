import json
from datetime import UTC, datetime, timedelta

import responses

from canvas_reminder.web import worker
from canvas_reminder.web.worker import WebContext, dispatch, run_for_user, send_digest_if_due
from tests.conftest import NOW, sample_ics

FEED_A = "https://canvas.example.edu/feeds/calendars/user_FAKEtokenAAAAAAAAAAAAAAAAAAAA.ics"
FEED_B = "https://canvas.example.edu/feeds/calendars/user_FAKEtokenBBBBBBBBBBBBBBBBBBBB.ics"


def setup_user(web: WebContext, uid: str, feed: str, channels: list[str]) -> None:
    web.users.ensure(uid, f"{uid}@example.edu", NOW)
    web.users.set_feed(uid, feed, NOW)
    web.users.update_preferences(
        uid, channels=channels, lead_times=["2d", "2h"], digest="off", paused=False, now=NOW
    )


def test_dispatch_queues_one_message_per_active_user(web: WebContext) -> None:
    for i in range(12):
        setup_user(web, f"user{i:02}", FEED_A, ["email"])
    web.users.ensure("no-feed", "x@example.edu", NOW)
    assert dispatch(web) == 12
    queued = []
    while batch := web.sqs.receive_message(QueueUrl=web.queue_url, MaxNumberOfMessages=10).get(
        "Messages"
    ):
        queued += [json.loads(m["Body"])["user"] for m in batch]
        for m in batch:
            web.sqs.delete_message(QueueUrl=web.queue_url, ReceiptHandle=m["ReceiptHandle"])
    assert sorted(queued) == [f"user{i:02}" for i in range(12)]


@responses.activate
def test_users_are_isolated_and_runs_are_idempotent(web: WebContext) -> None:
    responses.get(FEED_A, body=sample_ics())
    responses.get(FEED_B, body=sample_ics("sample_due_changed.ics"))
    responses.post("https://ntfy.sh")
    setup_user(web, "alice", FEED_A, ["email", "ntfy"])
    setup_user(web, "bob", FEED_B, ["email"])

    run_for_user(web, "alice", NOW)
    run_for_user(web, "bob", NOW)
    assert web.ses.to("alice@example.edu") == ["Canvas reminders are on: tracking 5 assignments"]
    assert web.ses.to("bob@example.edu") == ["Canvas reminders are on: tracking 5 assignments"]
    ntfy_posts = [c for c in responses.calls if c.request.method == "POST"]
    assert len(ntfy_posts) == 1  # only alice chose ntfy
    alice_topic = web.users.get("alice").ntfy_topic  # type: ignore[union-attr]
    assert json.loads(ntfy_posts[0].request.body)["topic"] == alice_topic

    # Each user has their own state: Bob's due date differs from Alice's.
    alice_due = web.repo("alice").get("event-assignment-100001").due  # type: ignore[union-attr]
    bob_due = web.repo("bob").get("event-assignment-100001").due  # type: ignore[union-attr]
    assert bob_due - alice_due == timedelta(days=3)

    run_for_user(web, "alice", NOW + timedelta(minutes=30))
    run_for_user(web, "bob", NOW + timedelta(minutes=30))
    assert len(web.ses.sent) == 2  # second run: nothing new
    assert web.users.get("alice").last_run_at is not None  # type: ignore[union-attr]


@responses.activate
def test_a_broken_feed_is_recorded_for_the_user_not_raised(web: WebContext) -> None:
    responses.get(FEED_A, status=404)
    setup_user(web, "alice", FEED_A, ["email"])
    assert run_for_user(web, "alice", NOW) == {}
    error = web.users.get("alice").last_error  # type: ignore[union-attr]
    assert "HTTP 404" in error and "FAKEtoken" not in error  # type: ignore[operator]


def test_paused_or_unknown_users_are_skipped(web: WebContext) -> None:
    setup_user(web, "alice", FEED_A, ["email"])
    web.users.update_preferences(
        "alice", channels=["email"], lead_times=["1d"], digest="off", paused=True, now=NOW
    )
    assert run_for_user(web, "alice", NOW) == {}
    assert run_for_user(web, "nobody", NOW) == {}


@responses.activate
def test_worker_handler_reports_only_unexpected_failures(web: WebContext, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    responses.get(FEED_A, body=sample_ics())
    setup_user(web, "alice", FEED_A, ["email"])
    monkeypatch.setattr(worker, "_context", web)
    event = {
        "Records": [
            {"messageId": "ok", "body": json.dumps({"user": "alice"})},
            {"messageId": "bad", "body": "not json"},
        ]
    }
    assert worker.worker_handler(event, None) == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


@responses.activate
def test_daily_summary_sent_once_at_8am_melbourne(web: WebContext) -> None:
    responses.get(FEED_A, body=sample_ics())
    setup_user(web, "alice", FEED_A, ["email"])
    web.users.update_preferences(
        "alice", channels=["email"], lead_times=["2h"], digest="daily", paused=False, now=NOW
    )
    eight_am = datetime(2026, 10, 22, 21, 10, tzinfo=UTC)  # Fri 23 Oct, 8:10 am AEDT
    run_for_user(web, "alice", eight_am)
    run_for_user(web, "alice", eight_am + timedelta(minutes=30))  # 8:40, same day
    run_for_user(web, "alice", eight_am + timedelta(hours=2))  # 10:10, not digest hour
    summaries = [s for s in web.ses.subjects if "summary" in s]
    assert summaries == ["Your daily Canvas summary: 1 due in the next 2 days"]


@responses.activate
def test_weekly_summary_only_on_monday(web: WebContext) -> None:
    responses.get(FEED_A, body=sample_ics())
    setup_user(web, "alice", FEED_A, ["email"])
    web.users.update_preferences(
        "alice", channels=["email"], lead_times=["2h"], digest="weekly", paused=False, now=NOW
    )
    settings = web.users.get("alice")
    assert settings is not None
    run_for_user(web, "alice", NOW)
    deps = worker.Deps(
        config=settings.to_config(web.ntfy_server),
        repo=web.repo("alice"),
        notifiers=web.notifiers(settings),
        fetch_feed=lambda: "",
    )
    friday_8am = datetime(2026, 10, 22, 21, 10, tzinfo=UTC)
    monday_8am = datetime(2026, 10, 25, 21, 10, tzinfo=UTC)  # Mon 26 Oct, 8:10 am AEDT
    assert not send_digest_if_due(web, settings, deps, friday_8am)
    assert send_digest_if_due(web, settings, deps, monday_8am)
    assert not send_digest_if_due(web, settings, deps, monday_8am + timedelta(minutes=30))

import json
from datetime import timedelta
from urllib.parse import parse_qs

import pytest
import responses
from responses import matchers

from canvas_reminder.config import Config
from canvas_reminder.http_client import HttpError, make_session
from canvas_reminder.models import Message
from canvas_reminder.notifiers import build_notifiers, gcal, mstodo
from canvas_reminder.notifiers.gcal import GoogleCalendarNotifier, event_id_for
from canvas_reminder.notifiers.mstodo import MicrosoftTodoNotifier
from canvas_reminder.notifiers.ntfy import NtfyNotifier
from canvas_reminder.notifiers.oauth import OAuthToken
from canvas_reminder.secrets import SecretStore
from tests.conftest import make_assignment, stored

LEADS = (timedelta(days=2), timedelta(hours=2))
RECORD = stored(make_assignment("event-assignment-42"))
EVENTS = f"{gcal.API}/calendars/primary/events"
LISTS = f"{mstodo.GRAPH}/me/todo/lists"
TASKS = f"{LISTS}/list1/tasks"


def session():  # type: ignore[no-untyped-def]
    return make_session(retries=0)


def body(call_index: int) -> dict:
    return json.loads(responses.calls[call_index].request.body)


def google_token() -> OAuthToken:
    params = {"client_id": "cid", "client_secret": "csecret", "refresh_token": "rtoken"}
    return OAuthToken(session(), gcal.TOKEN_URL, params)


# --- ntfy ------------------------------------------------------------------------------


@responses.activate
def test_ntfy_publishes_json_to_server_root() -> None:
    responses.post("https://ntfy.sh")
    message = Message("Due in 2 hours: Essay ✍️", "Body", ("alarm_clock",), 4, "https://x.test/a")
    NtfyNotifier(session(), "https://ntfy.sh/", "secret-topic", token="tk_abc").send(message)
    assert body(0) == {
        "topic": "secret-topic",
        "title": "Due in 2 hours: Essay ✍️",
        "message": "Body",
        "tags": ["alarm_clock"],
        "priority": 4,
        "click": "https://x.test/a",
    }
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tk_abc"


@responses.activate
def test_ntfy_error_hides_topic() -> None:
    responses.post("https://ntfy.sh", status=429)
    with pytest.raises(HttpError) as excinfo:
        NtfyNotifier(session(), "https://ntfy.sh", "secret-topic").send(Message("t", "b"))
    assert excinfo.value.status == 429 and "secret-topic" not in str(excinfo.value)


# --- OAuth -----------------------------------------------------------------------------


@responses.activate
def test_access_token_is_cached_until_expiry_and_rotation_is_reported() -> None:
    responses.post(
        gcal.TOKEN_URL, json={"access_token": "a1", "expires_in": 3600, "refresh_token": "r2"}
    )
    responses.post(gcal.TOKEN_URL, json={"access_token": "a2", "expires_in": 3600})
    clock = [0.0]
    rotated: list[str] = []
    params = {"client_id": "cid", "refresh_token": "r1"}
    token = OAuthToken(session(), gcal.TOKEN_URL, params, rotated.append, clock=lambda: clock[0])

    assert token.headers() == {"Authorization": "Bearer a1"}
    assert token.headers() == {"Authorization": "Bearer a1"}  # cached
    assert rotated == ["r2"]
    clock[0] = 3600
    assert token.headers() == {"Authorization": "Bearer a2"}
    sent = parse_qs(responses.calls[1].request.body)
    assert sent["refresh_token"] == ["r2"] and sent["grant_type"] == ["refresh_token"]


# --- Google Calendar -------------------------------------------------------------------


@responses.activate
def test_gcal_upsert_puts_deterministic_event_with_popup_reminders() -> None:
    responses.post(gcal.TOKEN_URL, json={"access_token": "a1", "expires_in": 3600})
    event_id = event_id_for(RECORD.uid)
    responses.put(
        f"{EVENTS}/{event_id}", match=[matchers.header_matcher({"Authorization": "Bearer a1"})]
    )

    assert (
        GoogleCalendarNotifier(session(), "primary", google_token()).upsert(RECORD, LEADS, None)
        == event_id
    )
    event = body(1)
    assert event["id"] == event_id and event["summary"] == "Due: Essay [ABC10001_2026_SM2]"
    assert event["start"] == {
        "dateTime": "2026-10-24T23:59:00+11:00",
        "timeZone": "Australia/Melbourne",
    }
    assert event["reminders"]["overrides"] == [
        {"method": "popup", "minutes": 2880},
        {"method": "popup", "minutes": 120},
    ]
    assert RECORD.url in event["description"]


def test_gcal_event_ids_are_valid_and_stable() -> None:
    event_id = event_id_for("event-assignment-42")
    assert event_id == event_id_for("event-assignment-42") != event_id_for("event-assignment-43")
    assert set(event_id) <= set("abcdefghijklmnopqrstuv0123456789") and 5 <= len(event_id) <= 1024


@responses.activate
def test_gcal_upsert_inserts_when_event_is_missing() -> None:
    responses.post(gcal.TOKEN_URL, json={"access_token": "a1"})
    responses.put(f"{EVENTS}/{event_id_for(RECORD.uid)}", status=404)
    responses.post(EVENTS)
    GoogleCalendarNotifier(session(), "primary", google_token()).upsert(RECORD, LEADS, None)
    assert responses.calls[2].request.method == "POST"


@responses.activate
def test_gcal_errors_and_delete() -> None:
    responses.post(gcal.TOKEN_URL, json={"access_token": "a1"})
    responses.put(f"{EVENTS}/e1", status=403)
    responses.delete(f"{EVENTS}/e1", status=410)
    responses.delete(f"{EVENTS}/e2", status=500)
    notifier = GoogleCalendarNotifier(session(), "primary", google_token())
    with pytest.raises(HttpError):
        notifier.upsert(RECORD, LEADS, "e1")
    notifier.delete("e1")  # already gone is fine
    with pytest.raises(HttpError):
        notifier.delete("e2")


# --- Microsoft To Do -------------------------------------------------------------------


def todo() -> MicrosoftTodoNotifier:
    responses.post(mstodo.TOKEN_URL, json={"access_token": "m1", "expires_in": 3600})
    params = {"client_id": "cid", "refresh_token": "r1", "scope": mstodo.SCOPES}
    return MicrosoftTodoNotifier(
        session(), "Canvas", OAuthToken(session(), mstodo.TOKEN_URL, params)
    )


@responses.activate
def test_todo_creates_list_and_task_with_linked_resource() -> None:
    notifier = todo()
    responses.get(LISTS, json={"value": [{"id": "other", "displayName": "Tasks"}]})
    responses.post(LISTS, json={"id": "list1"})
    responses.get(f"{TASKS}?$expand=linkedResources", json={"value": []})
    responses.post(TASKS, json={"id": "task1"})

    assert notifier.upsert(RECORD, LEADS, None) == "task1"
    task = json.loads(responses.calls[-1].request.body)
    assert task["title"] == "Essay [ABC10001_2026_SM2]"
    assert task["dueDateTime"] == {"dateTime": "2026-10-24T23:59:00", "timeZone": mstodo.GRAPH_TZ}
    assert task["reminderDateTime"]["dateTime"] == "2026-10-22T23:59:00"  # longest lead
    assert task["linkedResources"][0]["externalId"] == RECORD.uid


@responses.activate
def test_todo_finds_task_left_by_a_crashed_run_instead_of_duplicating() -> None:
    notifier = todo()
    responses.get(LISTS, json={"value": [], "@odata.nextLink": f"{LISTS}?page=2"})
    responses.get(f"{LISTS}?page=2", json={"value": [{"id": "list1", "displayName": "Canvas"}]})
    responses.get(
        f"{TASKS}?$expand=linkedResources",
        json={"value": [{"id": "task9", "linkedResources": [{"externalId": RECORD.uid}]}]},
    )
    responses.patch(f"{TASKS}/task9")
    assert notifier.upsert(RECORD, LEADS, None) == "task9"
    assert not any(c.request.method == "POST" and c.request.url == TASKS for c in responses.calls)


@responses.activate
def test_todo_recreates_a_task_the_user_deleted_and_delete_is_idempotent() -> None:
    notifier = todo()
    responses.get(LISTS, json={"value": [{"id": "list1", "displayName": "Canvas"}]})
    responses.patch(f"{TASKS}/gone", status=404)
    responses.post(TASKS, json={"id": "task2"})
    responses.delete(f"{TASKS}/task2", status=404)
    responses.patch(f"{TASKS}/locked", status=403)
    assert notifier.upsert(RECORD, LEADS, "gone") == "task2"
    notifier.delete("task2")
    with pytest.raises(HttpError):
        notifier.upsert(RECORD, LEADS, "locked")


# --- registry --------------------------------------------------------------------------


class FakeSecretsClient:
    def __init__(self) -> None:
        self.puts: list[tuple[str, str]] = []

    def get_parameter(self, Name: str, WithDecryption: bool) -> dict:
        raise AssertionError(f"unexpected AWS call for {Name}")

    def put_parameter(self, Name: str, Value: str, **kwargs: object) -> None:
        assert kwargs["Type"] == "SecureString" and kwargs["Tier"] == "Standard"
        self.puts.append((Name, Value))


@responses.activate
def test_build_notifiers_only_loads_enabled_channels_and_persists_rotation() -> None:
    env = {
        "CANVAS_REMINDER_NTFY": '{"topic": "t0pic-secret"}',
        "CANVAS_REMINDER_GOOGLE": '{"client_id": "c", "client_secret": "s", "refresh_token": "r"}',
        "CANVAS_REMINDER_MICROSOFT": '{"client_id": "c", "refresh_token": "old-refresh"}',
    }
    client = FakeSecretsClient()
    secrets = SecretStore(client=client, env=env)
    config = Config(channels=("ntfy",))
    assert [n.name for n in build_notifiers(config, secrets, session())] == ["ntfy"]

    all_on = Config(channels=("ntfy", "gcal", "mstodo"))
    names = [n.name for n in build_notifiers(all_on, secrets, session())]
    assert names == ["ntfy", "gcal", "mstodo"]

    # Rotation on a Parameter Store-backed secret is written back.
    stored_secret = SecretStore(client=client, env={})
    stored_secret._cache["microsoft"] = env["CANVAS_REMINDER_MICROSOFT"]
    [todo_notifier] = build_notifiers(Config(channels=("mstodo",)), stored_secret, session())
    responses.post(mstodo.TOKEN_URL, json={"access_token": "m1", "refresh_token": "new-refresh"})
    todo_notifier._token.headers()  # type: ignore[attr-defined]
    assert client.puts[0][0] == "/canvas-reminder/microsoft"
    assert json.loads(client.puts[0][1])["refresh_token"] == "new-refresh"

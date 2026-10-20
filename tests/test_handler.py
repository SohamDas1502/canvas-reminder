"""The Lambda entry point with real wiring: moto (Parameter Store + DynamoDB) and mocked HTTP."""

import json
import logging

import boto3
import pytest
import responses

from canvas_reminder import handler
from canvas_reminder.config import Config
from tests.conftest import sample_ics

FEED_URL = "https://canvas.example.edu/feeds/calendars/user_FAKEtoken1234567890abcdef.ics"
TOPIC = "fake-private-topic-8f3k2"


@pytest.fixture
def lambda_env(table, monkeypatch):  # type: ignore[no-untyped-def]
    ssm = boto3.client("ssm")
    ssm.put_parameter(Name="/canvas-reminder/ics-url", Value=FEED_URL, Type="SecureString")
    ssm.put_parameter(
        Name="/canvas-reminder/ntfy", Value=json.dumps({"topic": TOPIC}), Type="SecureString"
    )
    monkeypatch.setenv("TABLE_NAME", table.name)
    monkeypatch.setattr(handler, "_deps", None)
    # Independent of whatever the user has put in the bundled config.yaml.
    monkeypatch.setattr(handler, "load_config", lambda: Config(channels=("ntfy",)))
    return table


@responses.activate
def test_handler_runs_twice_without_duplicates_and_never_logs_secrets(lambda_env, caplog) -> None:  # type: ignore[no-untyped-def]
    responses.get(FEED_URL, body=sample_ics())
    responses.post("https://ntfy.sh")
    caplog.set_level(logging.DEBUG)

    first = handler.lambda_handler({}, None)
    second = handler.lambda_handler({}, None)

    assert first["new"] >= 1 and "new" not in second
    ntfy_posts = [c for c in responses.calls if c.request.method == "POST"]
    assert len(ntfy_posts) == 1  # the bootstrap summary, once
    assert json.loads(ntfy_posts[0].request.body)["topic"] == TOPIC

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Run summary" in logged
    assert "FAKEtoken" not in logged and TOPIC not in logged


@responses.activate
def test_handler_failure_is_sanitised(lambda_env, caplog) -> None:  # type: ignore[no-untyped-def]
    responses.get(FEED_URL, status=500)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(handler.RunFailed) as excinfo:
        handler.lambda_handler({}, None)
    assert "HTTP 500" in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
    for text in (str(excinfo.value), caplog.text):
        assert "FAKEtoken" not in text


@responses.activate
def test_missing_channel_secret_skips_that_channel_only(lambda_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(handler, "load_config", lambda: Config(channels=("ntfy", "gcal")))
    responses.get(FEED_URL, body=sample_ics())
    responses.post("https://ntfy.sh")
    with pytest.raises(handler.RunFailed) as excinfo:
        handler.lambda_handler({}, None)
    assert "gcal skipped" in str(excinfo.value)
    assert len([c for c in responses.calls if c.request.method == "POST"]) == 1  # ntfy still sent

import json

import boto3
import pytest
from moto import mock_aws

from canvas_reminder.redact import redact
from canvas_reminder.secrets import SecretStore


@pytest.fixture
def ssm():  # type: ignore[no-untyped-def]
    with mock_aws():
        yield boto3.client("ssm")


def test_reads_and_decrypts_securestring_parameters(ssm) -> None:  # type: ignore[no-untyped-def]
    ssm.put_parameter(
        Name="/canvas-reminder/ntfy", Value='{"topic": "hush-hush-topic"}', Type="SecureString"
    )
    store = SecretStore(env={})
    assert store.get_json("ntfy") == {"topic": "hush-hush-topic"}
    assert "hush-hush-topic" not in redact("posting to hush-hush-topic")  # registered


def test_values_are_cached_per_container(ssm) -> None:  # type: ignore[no-untyped-def]
    ssm.put_parameter(
        Name="/canvas-reminder/ics-url", Value="https://x.test/a.ics", Type="SecureString"
    )
    store = SecretStore(env={})
    assert store.get("ics-url") == "https://x.test/a.ics"
    ssm.put_parameter(
        Name="/canvas-reminder/ics-url",
        Value="https://x.test/b.ics",
        Type="SecureString",
        Overwrite=True,
    )
    assert store.get("ics-url") == "https://x.test/a.ics"


def test_put_json_overwrites_as_standard_securestring(ssm) -> None:  # type: ignore[no-untyped-def]
    ssm.put_parameter(
        Name="/canvas-reminder/microsoft", Value='{"refresh_token": "old"}', Type="SecureString"
    )
    store = SecretStore(env={})
    store.put_json("microsoft", {"refresh_token": "new-token-123"})
    param = ssm.get_parameter(Name="/canvas-reminder/microsoft", WithDecryption=True)["Parameter"]
    assert json.loads(param["Value"]) == {"refresh_token": "new-token-123"}
    assert param["Type"] == "SecureString"
    tier = ssm.describe_parameters()["Parameters"][0].get("Tier", "Standard")
    assert tier == "Standard"


def test_environment_overrides_and_is_never_written_back() -> None:
    store = SecretStore(env={"CANVAS_REMINDER_NTFY": '{"topic": "local"}'}, client=object())
    assert store.get_json("ntfy") == {"topic": "local"}
    store.put_json("ntfy", {"topic": "changed"})  # warns, no AWS call (client has no methods)
    assert store.get_json("ntfy") == {"topic": "changed"}


def test_non_object_json_is_rejected() -> None:
    store = SecretStore(env={"CANVAS_REMINDER_NTFY": '["not", "an", "object"]'})
    with pytest.raises(ValueError, match="/canvas-reminder/ntfy"):
        store.get_json("ntfy")

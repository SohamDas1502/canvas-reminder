import logging
import sys

import pytest

from canvas_reminder.redact import REDACTED, RedactingFilter, redact, register_secret

FEED = "https://canvas.example.edu/feeds/calendars/user_FAKEtoken1234567890abcdef.ics"


def test_registered_secret_and_its_path_are_masked() -> None:
    register_secret(FEED)
    assert "FAKEtoken" not in redact(f"fetching {FEED}")
    assert "FAKEtoken" not in redact('GET "/feeds/calendars/user_FAKEtoken1234567890abcdef.ics"')


def test_short_values_are_not_registered() -> None:
    register_secret("abc")
    assert redact("abc") == "abc"


@pytest.mark.parametrize(
    "line",
    [
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secretpart",
        '{"refresh_token": "1//0gSecretSecret"}',
        "client_secret=GOCSPX-secretvalue&x=1",
        "posted to https://ntfy.sh/my-private-topic-xyz",
    ],
)
def test_patterns_are_masked(line: str) -> None:
    out = redact(line)
    assert REDACTED in out
    for secret in ("secretpart", "SecretSecret", "secretvalue", "my-private-topic"):
        assert secret not in out


def test_url_without_path_is_left_alone() -> None:
    assert redact("see https://ntfy.sh") == "see https://ntfy.sh"


def test_filter_redacts_args_and_tracebacks() -> None:
    register_secret("super-secret-value")
    try:
        raise RuntimeError("failed with super-secret-value")
    except RuntimeError:
        record = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "token is %s", ("super-secret-value",), None
        )
        record.exc_info = sys.exc_info()
    assert RedactingFilter().filter(record)
    assert "super-secret-value" not in record.getMessage()
    assert "RuntimeError" in record.getMessage() and record.exc_info is None

from datetime import timedelta

import pytest

from canvas_reminder.config import ConfigError, config_from_dict, load_config, parse_duration


def test_bundled_config_is_valid() -> None:
    config = load_config()  # whatever `make configure` wrote must load cleanly
    assert config.channels and config.lead_times


def test_defaults() -> None:
    config = config_from_dict({})
    assert config.channels == ("ntfy",)
    assert config.lead_times == (timedelta(days=2), timedelta(hours=2))
    assert config.on_removed == "notify"


def test_overrides_and_ignores_match_course_prefix() -> None:
    config = config_from_dict(
        {
            "lead_times": ["2h", "2d"],
            "course_overrides": {"abc10001": {"lead_times": ["1d", "5d"]}},
            "ignore_courses": ["SKIP"],
        }
    )
    assert config.leads_for("ABC10001_2026_SM2") == (timedelta(days=5), timedelta(days=1))
    assert config.leads_for("XYZ20002_2026_SM2") == (timedelta(days=2), timedelta(hours=2))
    assert config.is_ignored("SKIP30003_2026_SM2")
    assert not config.is_ignored("ABC10001_2026_SM2")


@pytest.mark.parametrize(("text", "delta"), [("30m", timedelta(minutes=30)), (" 2D", timedelta(2))])
def test_parse_duration(text: str, delta: timedelta) -> None:
    assert parse_duration(text) == delta


@pytest.mark.parametrize(
    "raw",
    [
        {"channels": ["sms"]},
        {"on_removed": "explode"},
        {"lead_times": []},
        {"lead_times": ["2w"]},
        {"lead_times": ["0h"]},
        {"lead_times": ["29d"]},
        {"lead_times": ["1h", "2h", "3h", "4h", "5h", "6h"]},
        {"lead_time": ["2d"]},  # typo'd key
    ],
)
def test_invalid_config_is_rejected(raw: dict) -> None:
    with pytest.raises(ConfigError):
        config_from_dict(raw)

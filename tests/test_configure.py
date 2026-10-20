from datetime import timedelta

import yaml

from canvas_reminder.config import Config, config_from_dict
from scripts.configure import Prompter, describe, lead_text, render_config, run_wizard


def wizard(answers: list[str], current: Config | None = None) -> tuple[Config, list[str]]:
    replies = iter(answers)
    said: list[str] = []
    prompter = Prompter(ask=lambda _q: next(replies), say=said.append)
    raw = run_wizard(prompter, current or Config())
    return config_from_dict(yaml.safe_load(render_config(raw))), said


def test_wizard_sets_times_courses_and_channels() -> None:
    config, said = wizard(
        [
            "1w",  # invalid unit -> asked again
            "3d, 1d 2h",  # lead times
            "y",  # course-specific times?
            "comp90024",
            "5d, 1d",
            "",  # done with courses
            "skip3, UNIB",  # ignore
            "",
            "y",
            "n",
            "y",  # ntfy (keep default yes), gcal, mstodo, apple
            "",  # on_removed: keep default
        ]
    )
    assert config.lead_times == (timedelta(days=3), timedelta(days=1), timedelta(hours=2))
    assert config.leads_for("COMP90024_2026_SM2") == (timedelta(days=5), timedelta(days=1))
    assert config.ignore_courses == ("SKIP3", "UNIB")
    assert config.channels == ("ntfy", "gcal", "apple")
    assert config.on_removed == "notify"
    assert any("Example: 3d, 1d, 2h" in line for line in said)
    assert any("3 days, 1 day and 2 hours" in line for line in said)


def test_pressing_enter_keeps_everything() -> None:
    current = Config(channels=("ntfy", "mstodo"), lead_times=(timedelta(days=1),))
    config, _ = wizard(["", "n", "", "", "", "", "", ""], current)
    assert config.lead_times == (timedelta(days=1),)
    assert config.channels == ("ntfy", "mstodo")


def test_at_least_one_channel_is_required() -> None:
    config, said = wizard(["", "n", "", "n", "n", "n", "n", "y", "", "", "", "delete"])
    assert config.channels == ("ntfy",) and config.on_removed == "delete"
    assert "  Pick at least one." in said


def test_lead_text_and_describe() -> None:
    assert [
        lead_text(x) for x in (timedelta(days=2), timedelta(hours=5), timedelta(minutes=90))
    ] == ["2d", "5h", "90m"]
    assert describe([timedelta(days=2)]) == "2 days"

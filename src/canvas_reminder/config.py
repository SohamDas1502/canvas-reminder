"""Load and validate config.yaml (channels, lead times, per-course overrides, ignores)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).with_name("config.yaml")
# ntfy, gcal, mstodo and email are pushed to by the pipeline (email: web app only);
# apple is pulled by an iPhone Shortcut.
CHANNELS = ("ntfy", "gcal", "mstodo", "apple", "email")
REMOVED_ACTIONS = ("notify", "delete", "ignore")
KNOWN_KEYS = {
    "channels",
    "lead_times",
    "course_overrides",
    "ignore_courses",
    "on_removed",
    "ntfy_server",
    "google_calendar_id",
    "todo_list_name",
}
MAX_LEADS = 5  # Google Calendar allows at most 5 reminder overrides per event
MAX_LEAD = timedelta(days=28)  # ... each at most 4 weeks before the event
_DURATION = re.compile(r"^(\d+)\s*([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    channels: tuple[str, ...] = ("ntfy",)
    lead_times: tuple[timedelta, ...] = (timedelta(days=2), timedelta(hours=2))
    course_lead_times: Mapping[str, tuple[timedelta, ...]] | None = None
    ignore_courses: tuple[str, ...] = ()
    on_removed: str = "notify"
    ntfy_server: str = "https://ntfy.sh"
    google_calendar_id: str = "primary"
    todo_list_name: str = "Canvas"

    def leads_for(self, course: str) -> tuple[timedelta, ...]:
        """Lead times for a course: the first override whose key prefixes the code wins."""
        for prefix, leads in (self.course_lead_times or {}).items():
            if course.upper().startswith(prefix.upper()):
                return leads
        return self.lead_times

    def is_ignored(self, course: str) -> bool:
        return any(course.upper().startswith(p.upper()) for p in self.ignore_courses)


def load_config(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("CONFIG_PATH", DEFAULT_PATH))
    return config_from_dict(yaml.safe_load(path.read_text()) or {})


def config_from_dict(raw: Mapping[str, Any]) -> Config:
    unknown = set(raw) - KNOWN_KEYS
    if unknown:
        raise ConfigError(f"Unknown config keys: {sorted(unknown)}")

    channels = tuple(raw.get("channels", Config.channels))
    bad = [c for c in channels if c not in CHANNELS]
    if bad:
        raise ConfigError(f"Unknown channels {bad}; choose from {list(CHANNELS)}")

    on_removed = raw.get("on_removed", Config.on_removed)
    if on_removed not in REMOVED_ACTIONS:
        raise ConfigError(f"on_removed must be one of {list(REMOVED_ACTIONS)}")

    overrides = {
        str(course): parse_leads((spec or {}).get("lead_times", []))
        for course, spec in (raw.get("course_overrides") or {}).items()
    }
    return Config(
        channels=channels,
        lead_times=parse_leads(raw.get("lead_times", ["2d", "2h"])),
        course_lead_times=overrides,
        ignore_courses=tuple(str(c) for c in raw.get("ignore_courses") or ()),
        on_removed=on_removed,
        ntfy_server=str(raw.get("ntfy_server", Config.ntfy_server)).rstrip("/"),
        google_calendar_id=str(raw.get("google_calendar_id", Config.google_calendar_id)),
        todo_list_name=str(raw.get("todo_list_name", Config.todo_list_name)),
    )


def parse_leads(values: list[str]) -> tuple[timedelta, ...]:
    """['2h', '2d'] -> (2 days, 2 hours): unique, longest first."""
    leads = sorted({parse_duration(str(v)) for v in values}, reverse=True)
    if not leads or len(leads) > MAX_LEADS:
        raise ConfigError(f"Give between 1 and {MAX_LEADS} lead times")
    if leads[0] > MAX_LEAD:
        raise ConfigError("Lead times can be at most 28d (Google Calendar's limit)")
    return tuple(leads)


def format_duration(lead: timedelta) -> str:
    """timedelta -> '2d' / '2h' / '30m': the inverse of parse_duration."""
    seconds = int(lead.total_seconds())
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def parse_duration(text: str) -> timedelta:
    match = _DURATION.match(text.strip().lower())
    if not match or int(match[1]) == 0:
        raise ConfigError(f"Bad duration {text!r}; use e.g. 30m, 2h or 2d")
    return timedelta(**{_UNITS[match[2]]: int(match[1])})

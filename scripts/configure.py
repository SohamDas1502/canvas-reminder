"""Interactive setup: choose how long before each due date you're reminded, and where.

    make configure

Writes src/canvas_reminder/config.yaml, checks the secrets each chosen channel needs,
and optionally deploys. Every channel uses the same reminder times.
"""

from __future__ import annotations

import re
import secrets
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from canvas_reminder.config import (  # noqa: E402
    DEFAULT_PATH,
    REMOVED_ACTIONS,
    Config,
    ConfigError,
    config_from_dict,
    format_duration,
    load_config,
    parse_leads,
)
from canvas_reminder.schedule import humanize  # noqa: E402

CHANNEL_NAMES = {
    "ntfy": "ntfy push notifications",
    "gcal": "Google Calendar",
    "mstodo": "Microsoft To Do",
    "apple": "Apple Reminders (via an iPhone Shortcut)",
}
SECRET_FOR = {"ntfy": "ntfy", "gcal": "google", "mstodo": "microsoft", "apple": "apple"}
_COURSE = re.compile(r"^[A-Za-z0-9_-]+$")


lead_text = format_duration  # the format config.yaml uses, e.g. '2d'


def describe(leads: Sequence[timedelta]) -> str:
    """(2d, 2h) -> '2 days and 2 hours'."""
    words = [humanize(lead) for lead in leads]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


class Prompter:
    def __init__(
        self, ask: Callable[[str], str] = input, say: Callable[[str], None] = print
    ) -> None:
        self._ask = ask
        self.say = say

    def text(self, question: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        return self._ask(f"{question}{suffix}: ").strip() or default

    def yes(self, question: str, default: bool) -> bool:
        answer = self._ask(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not answer else answer.startswith("y")

    def choice(self, question: str, options: Sequence[str], default: str) -> str:
        while True:
            answer = self.text(f"{question} ({'/'.join(options)})", default).lower()
            if answer in options:
                return answer
            self.say(f"  Please type one of: {', '.join(options)}")

    def leads(self, question: str, default: Sequence[timedelta]) -> tuple[timedelta, ...]:
        while True:
            answer = self.text(question, ", ".join(lead_text(x) for x in default))
            try:
                return parse_leads([part for part in re.split(r"[,\s]+", answer) if part])
            except ConfigError as exc:
                self.say(f"  {exc}. Example: 3d, 1d, 2h")


def run_wizard(p: Prompter, current: Config) -> dict[str, Any]:
    """Ask the questions and return a raw config dict (what config.yaml will contain)."""
    p.say("\n1. When should I remind you?")
    leads = p.leads(
        "How long before each due date? Up to 5 times, e.g. 3d, 1d, 2h "
        "(d = days, h = hours, m = minutes)",
        current.lead_times,
    )
    p.say(f"  OK: {describe(leads)} before every due date, in every app you choose.")

    overrides: dict[str, tuple[timedelta, ...]] = dict(current.course_lead_times or {})
    if overrides:
        p.say(
            "  Current per-course times: "
            + "; ".join(f"{course}: {describe(v)}" for course, v in overrides.items())
        )
        if not p.yes("  Keep them?", True):
            overrides = {}
    if p.yes("Should any course use different times?", False):
        while True:
            course = p.text("  Course code, or its start (e.g. COMP90024); blank to finish")
            if not course:
                break
            if not _COURSE.match(course):
                p.say("  Letters, digits, _ and - only.")
                continue
            overrides[course.upper()] = p.leads(f"  Times for {course.upper()}", leads)

    ignore = p.text(
        "Courses to ignore, comma-separated (blank for none)", ", ".join(current.ignore_courses)
    )

    p.say("\n2. Where should reminders go?")
    while True:
        channels = [
            c for c in CHANNEL_NAMES if p.yes(f"  {CHANNEL_NAMES[c]}?", c in current.channels)
        ]
        if channels:
            break
        p.say("  Pick at least one.")

    p.say("\n3. If an assignment disappears from Canvas before it's due:")
    p.say(
        "  notify = tell me, keep the reminders · delete = tell me and delete them · "
        "ignore = do nothing"
    )
    on_removed = p.choice("  What should I do?", REMOVED_ACTIONS, current.on_removed)

    return {
        "channels": channels,
        "lead_times": [lead_text(x) for x in leads],
        "course_overrides": {
            course: {"lead_times": [lead_text(x) for x in v]} for course, v in overrides.items()
        },
        "ignore_courses": [c.strip().upper() for c in ignore.split(",") if c.strip()],
        "on_removed": on_removed,
        "ntfy_server": current.ntfy_server,
        "google_calendar_id": current.google_calendar_id,
        "todo_list_name": current.todo_list_name,
    }


def render_config(raw: dict[str, Any]) -> str:
    """config.yaml text, with comments, for a raw config dict."""

    def flow(items: Sequence[str]) -> str:
        return "[" + ", ".join(items) + "]"

    overrides = "".join(
        f"  {course}:\n    lead_times: {flow(spec['lead_times'])}\n"
        for course, spec in raw["course_overrides"].items()
    )
    return f"""# canvas-reminder-pipeline configuration (written by `make configure`).
# No secrets here: those live in SSM Parameter Store. Takes effect on the next deploy.

# Where reminders go: ntfy, gcal (Google Calendar), mstodo (Microsoft To Do),
# apple (Apple Reminders, via an iPhone Shortcut). Each needs its secret (see README).
channels: {flow(raw["channels"])}

# How long before the due date to remind you, in every channel (m, h, d; up to 5).
# To Do and Apple Reminders allow one alert per item, so their alert moves to the
# next of these times as each one passes.
lead_times: {flow(raw["lead_times"])}

# Per-course times. Keys match the start of the Canvas course code.
course_overrides:{" {}" if not overrides else chr(10) + overrides.rstrip()}

# Courses to skip entirely (same prefix matching).
ignore_courses: {flow(raw["ignore_courses"])}

# When an assignment disappears from Canvas before it's due: notify | delete | ignore
on_removed: {raw["on_removed"]}

ntfy_server: {raw["ntfy_server"]}
google_calendar_id: {raw["google_calendar_id"]}
todo_list_name: {raw["todo_list_name"]}
"""


def check_secrets(p: Prompter, channels: Sequence[str]) -> None:
    """Report missing secrets; offer to create the Apple Reminders token."""
    try:
        import boto3

        client = boto3.client("ssm", region_name="ap-southeast-2")
        existing = set()
        for page in client.get_paginator("describe_parameters").paginate(
            ParameterFilters=[{"Key": "Path", "Option": "OneLevel", "Values": ["/canvas-reminder"]}]
        ):
            existing |= {param["Name"] for param in page["Parameters"]}
    except Exception as exc:
        p.say(f"\n(Could not check AWS parameters: {type(exc).__name__}. Skipping.)")
        return

    for channel in ["ics-url", *channels]:
        name = f"/canvas-reminder/{SECRET_FOR.get(channel, channel)}"
        if name in existing:
            p.say(f"  ✓ {name}")
        elif channel == "apple" and p.yes(f"  ✗ {name} is missing. Create it now?", True):
            token = secrets.token_urlsafe(32)
            client.put_parameter(
                Name=name,
                Value=f'{{"token": "{token}"}}',
                Type="SecureString",
                Tier="Standard",
            )
            p.say("  Created. To copy the token into your iPhone Shortcut later, run:")
            p.say(f"    aws ssm get-parameter --name {name} --with-decryption \\")
            p.say("      --query Parameter.Value --output text --region ap-southeast-2")
        else:
            p.say(f"  ✗ {name} is missing: see the README setup section for this channel.")


def main() -> None:
    p = Prompter()
    p.say("canvas-reminder-pipeline setup. Press Enter to keep the value in [brackets].")
    raw = run_wizard(p, load_config())
    text = render_config(raw)
    config = config_from_dict(yaml.safe_load(text))  # validate before saving

    p.say(f"\nYou'll be reminded {describe(config.lead_times)} before each due date via:")
    for channel in config.channels:
        p.say(f"  • {CHANNEL_NAMES[channel]}")
    if not p.yes(f"Save to {DEFAULT_PATH.relative_to(ROOT)}?", True):
        p.say("Nothing saved.")
        return
    DEFAULT_PATH.write_text(text)
    p.say("Saved.\n\nChecking secrets in Parameter Store (ap-southeast-2):")
    check_secrets(p, config.channels)

    if p.yes("\nDeploy now?", False):
        deploy(p)
    else:
        p.say("Run `make deploy` when ready. Existing reminders update on the next run after.")


def deploy(p: Prompter) -> None:
    """`make deploy`, which reads your email settings from deploy.env."""
    if not (ROOT / "deploy.env").exists():
        p.say("First create deploy.env from deploy.env.example (your alarm email), then run")
        p.say("`make deploy`. Your config.yaml is saved.")
        return
    try:
        subprocess.run(["make", "deploy"], cwd=ROOT, check=True)
    except subprocess.CalledProcessError:
        p.say("\nDeploy did not finish (see the error above). Your config.yaml is saved;")
        p.say("fix the problem and run `make deploy`.")
        return
    p.say("Deployed. Existing reminders are updated to the new times on the next run.")


if __name__ == "__main__":
    main()

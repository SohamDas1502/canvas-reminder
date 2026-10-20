"""Local demo UI: the real pipeline, run against the fake sample feed.

DynamoDB is an in-memory moto mock and the channels render on screen, so nothing is
sent anywhere and no AWS account or secrets are needed.

    make demo        # then open http://127.0.0.1:8000
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import boto3  # noqa: E402
from configure import lead_text  # noqa: E402
from moto import mock_aws  # noqa: E402

from canvas_reminder.apple import build_feed  # noqa: E402
from canvas_reminder.config import Config, ConfigError, parse_leads  # noqa: E402
from canvas_reminder.models import Message, StoredAssignment  # noqa: E402
from canvas_reminder.notifiers import PushNotifier, ReminderNotifier  # noqa: E402
from canvas_reminder.pipeline import Deps, run  # noqa: E402
from canvas_reminder.schedule import format_local, humanize  # noqa: E402
from canvas_reminder.state import StateRepository  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
PAGE = Path(__file__).with_name("demo.html")
START = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)  # Tue 20 Oct 2026, 11:00 am AEDT
ESSAY = "event-assignment-100001"
EMPTY_FEED = "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"
EXTRA_EVENT = """BEGIN:VEVENT
DTSTAMP:20261015T000000Z
UID:event-assignment-100007
DTSTART:20261026T065900Z
DTEND:20261026T065900Z
SUMMARY:Problem Set 5 [DEMO10001_2026_SM2]
DESCRIPTION:Questions 1-6 from chapter 9.
URL;VALUE=URI:https://canvas.example.edu/courses/1001/assignments/100007
END:VEVENT
"""


class ScreenPush(PushNotifier):
    name = "ntfy"

    def __init__(self, demo: Demo) -> None:
        self.demo = demo

    def send(self, message: Message) -> None:
        self.demo.pushes.append(
            {
                "at": format_local(self.demo.now),
                "title": message.title,
                "body": message.body,
                "tags": list(message.tags),
                "priority": message.priority,
                "run": len(self.demo.runs) + 1,
            }
        )


class ScreenReminder(ReminderNotifier):
    def __init__(self, name: str, demo: Demo, single_alert: bool = False) -> None:
        self.name = name  # type: ignore[misc]
        self.single_alert = single_alert  # type: ignore[misc]
        self.demo = demo
        self.items: dict[str, dict[str, Any]] = {}

    def upsert(self, assignment: StoredAssignment, leads: Any, ref: str | None) -> str:
        if ref is None or ref not in self.items:
            ref = f"{self.name}-{len(self.items) + 1}"
            self.items[ref] = {"ref": ref, "updates": 0, "created_run": len(self.demo.runs) + 1}
        else:
            self.items[ref]["updates"] += 1
        self.items[ref].update(
            label=assignment.label,
            due=format_local(assignment.due),
            due_iso=assignment.due.isoformat(),
            leads=[humanize(lead) for lead in leads],
            reminder_at=format_local(assignment.due - max(leads)),
            touched_run=len(self.demo.runs) + 1,
            deleted=False,
        )
        return ref

    def delete(self, ref: str) -> None:
        if ref in self.items:
            self.items[ref].update(deleted=True, touched_run=len(self.demo.runs) + 1)


class Demo:
    def __init__(self) -> None:
        self._mock: Any = None
        self.reset()

    def reset(self) -> None:
        if self._mock:
            self._mock.stop()
        self._mock = mock_aws()
        self._mock.start()
        table = boto3.resource("dynamodb", region_name="ap-southeast-2").create_table(
            TableName="demo",
            KeySchema=[{"AttributeName": "uid", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "uid", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.repo = StateRepository(table)
        self.now = START
        self.due_moved = self.removed = self.added = self.glitch = False
        self.pushes: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.gcal = ScreenReminder("gcal", self)
        self.todo = ScreenReminder("mstodo", self, single_alert=True)
        config = Config(channels=("ntfy", "gcal", "mstodo", "apple"), ignore_courses=("SKIP",))
        self.deps = Deps(config, self.repo, [ScreenPush(self), self.gcal, self.todo], self.feed)

    # --- the fake Canvas feed ----------------------------------------------------------

    def feed(self) -> str:
        if self.glitch:
            self.glitch = False
            return EMPTY_FEED
        name = "sample_due_changed.ics" if self.due_moved else "sample.ics"
        text = (FIXTURES / name).read_text()
        if self.removed:
            start = text.index(f"UID:{ESSAY}")
            begin = text.rindex("BEGIN:VEVENT", 0, start)
            end = text.index("END:VEVENT", start) + len("END:VEVENT\n")
            text = text[:begin] + text[end:]
        if self.added:
            text = text.replace("END:VCALENDAR", EXTRA_EVENT + "END:VCALENDAR")
        return text

    def essay_due(self) -> datetime:
        return datetime(2026, 10, 27 if self.due_moved else 24, 12, 59, tzinfo=UTC)

    # --- actions -----------------------------------------------------------------------

    def act(self, action: str) -> None:
        if action == "reset":
            self.reset()
            return
        label = {
            "run": "Scheduled run",
            "rerun": "Same run again (simulated retry)",
        }.get(action, "")
        if action == "advance_30m":
            self.now += timedelta(minutes=30)
            label = "30 minutes later"
        elif action == "advance_day":
            self.now += timedelta(days=1)
            label = "1 day later"
        elif action in ("jump_2d", "jump_2h"):
            lead = timedelta(days=2) if action == "jump_2d" else timedelta(hours=2)
            self.now = max(self.now, self.essay_due() - lead + timedelta(minutes=1))
            label = f"Jump to {humanize(lead)} before Assignment 1 is due"
        elif action == "toggle_due":
            self.due_moved = not self.due_moved
            label = "Canvas: Assignment 1 due date " + (
                "moved to 27 Oct" if self.due_moved else "moved back to 24 Oct"
            )
        elif action == "toggle_remove":
            self.removed = not self.removed
            label = "Canvas: Assignment 1 " + ("removed" if self.removed else "restored")
        elif action == "toggle_add":
            self.added = not self.added
            label = "Canvas: Problem Set 5 " + ("published" if self.added else "unpublished")
        elif action == "glitch":
            self.glitch = True
            label = "Canvas returns an empty feed (glitch)"
        if not label:
            raise ValueError(action)
        self._run(label)

    def set_leads(self, text: str) -> None:
        """What `make configure` does, live: new lead times for every channel."""
        leads = parse_leads([part for part in re.split(r"[,\s]+", text) if part])
        self.deps.config = replace(self.deps.config, lead_times=leads)
        self._run(f"Reminder times set to {', '.join(humanize(x) for x in leads)} before")

    def _run(self, label: str) -> None:
        pushes_before = len(self.pushes)
        entry: dict[str, Any] = {
            "n": len(self.runs) + 1,
            "at": format_local(self.now),
            "label": label,
        }
        try:
            entry["summary"] = run(self.deps, self.now)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["pushes"] = len(self.pushes) - pushes_before
        self.runs.append(entry)

    # --- view model --------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        records = sorted(self.repo.load_all().values(), key=lambda r: r.due)
        config = self.deps.config
        return {
            "now": format_local(self.now),
            "leads": ", ".join(lead_text(x) for x in config.lead_times),
            "apple": build_feed(records, config, self.now)["items"] if self.runs else [],
            "flags": {
                "due_moved": self.due_moved,
                "removed": self.removed,
                "added": self.added,
            },
            "runs": self.runs[::-1],
            "last_run": len(self.runs),
            "pushes": self.pushes[::-1],
            "gcal": sorted(self.gcal.items.values(), key=lambda i: i["due_iso"]),
            "todo": sorted(self.todo.items.values(), key=lambda i: i["due_iso"]),
            "records": [
                {
                    "uid": r.uid,
                    "label": r.label,
                    "due": format_local(r.due),
                    "in": humanize(r.due - self.now) if r.due > self.now else "past due",
                    "version": r.version,
                    "status": r.status.value,
                    "sent": sorted(r.sent),
                    "refs": {ch: v["ref"] for ch, v in r.refs.items()},
                    "hash": r.content_hash[:8],
                }
                for r in records
            ],
        }


class Handler(BaseHTTPRequestHandler):
    demo: Demo

    def do_GET(self) -> None:
        if self.path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._json(self.demo.state())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        try:
            if self.path == "/api/leads":
                length = int(self.headers.get("Content-Length", 0))
                self.demo.set_leads(json.loads(self.rfile.read(length))["leads"])
            else:
                self.demo.act(self.path.removeprefix("/api/action/"))
        except ConfigError as exc:
            self._send(400, str(exc).encode(), "text/plain")
            return
        except ValueError:
            self._send(400, b"unknown action", "text/plain")
            return
        self._json(self.demo.state())

    def _json(self, value: Any) -> None:
        self._send(200, json.dumps(value).encode(), "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    Handler.demo = Demo()
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"Demo running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()

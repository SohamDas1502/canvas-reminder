"""Run the pipeline on your machine.

  Dry run (no AWS, nothing sent; DynamoDB is an in-memory moto mock):
    python scripts/run_local.py --ics tests/fixtures/sample.ics --dry-run --runs 2
    python scripts/run_local.py --dry-run \
        --ics tests/fixtures/sample.ics --ics tests/fixtures/sample_due_changed.ics

  Real run (secrets from .env, state in DynamoDB at DYNAMODB_ENDPOINT_URL, e.g. LocalStack):
    python scripts/run_local.py --create-table

Each --ics is used for one run, in order (the last one repeats for any extra --runs).
Without --ics the feed comes from CANVAS_REMINDER_ICS_URL in .env.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from canvas_reminder.config import load_config  # noqa: E402
from canvas_reminder.handler import build_deps  # noqa: E402
from canvas_reminder.http_client import make_session  # noqa: E402
from canvas_reminder.ics import FeedError, fetch_ics  # noqa: E402
from canvas_reminder.models import Message, StoredAssignment  # noqa: E402
from canvas_reminder.notifiers import Notifier, PushNotifier, ReminderNotifier  # noqa: E402
from canvas_reminder.pipeline import Deps, run  # noqa: E402
from canvas_reminder.redact import register_secret, setup_logging  # noqa: E402
from canvas_reminder.schedule import format_local  # noqa: E402
from canvas_reminder.state import StateRepository  # noqa: E402


class PrintPush(PushNotifier):
    name = "ntfy"

    def send(self, message: Message) -> None:
        print(
            f"  [push] {message.title}\n         {message.body.replace(chr(10), chr(10) + ' ' * 9)}"
        )


class PrintReminder(ReminderNotifier):
    def __init__(self, name: str) -> None:
        self.name = name  # type: ignore[misc]

    def upsert(
        self, assignment: StoredAssignment, leads: Sequence[timedelta], ref: str | None
    ) -> str:
        action = "update" if ref else "create"
        print(f"  [{self.name}] {action}: {assignment.label} @ {assignment.due:%Y-%m-%d %H:%M} UTC")
        return ref or f"dry-{assignment.uid}"

    def delete(self, ref: str) -> None:
        print(f"  [{self.name}] delete {ref}")


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Values are never printed."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#") and value:
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def create_table(resource: object, name: str) -> object:
    with contextlib.suppress(Exception):
        return resource.create_table(  # type: ignore[attr-defined]
            TableName=name,
            KeySchema=[{"AttributeName": "uid", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "uid", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    return resource.Table(name)  # type: ignore[attr-defined]


@contextlib.contextmanager
def dry_run_deps(feeds: Iterator[str]) -> Iterator[Deps]:
    import boto3
    from moto import mock_aws

    config = load_config()
    notifiers: list[Notifier] = [PrintPush()] + [
        PrintReminder(c) for c in config.channels if c in ("gcal", "mstodo")
    ]
    with mock_aws():
        table = create_table(boto3.resource("dynamodb", region_name="ap-southeast-2"), "dry-run")
        yield Deps(config, StateRepository(table), notifiers, lambda: next(feeds))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ics", action="append", type=Path, help="local ICS file (repeatable)")
    parser.add_argument(
        "--dry-run", action="store_true", help="in-memory state, print instead of send"
    )
    parser.add_argument(
        "--runs", type=int, default=None, help="number of runs (default: one per --ics)"
    )
    parser.add_argument("--now", help="pretend time, ISO 8601 (default: now)")
    parser.add_argument("--create-table", action="store_true", help="create TABLE_NAME if missing")
    parser.add_argument(
        "--ask-url",
        action="store_true",
        help="prompt (hidden) for your real feed URL and read it in memory; nothing is saved",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    setup_logging(os.environ.get("LOG_LEVEL", "WARNING"))
    files = args.ics or []
    runs = args.runs or max(len(files), 1)
    texts = [f.read_text() for f in files]
    if args.ask_url:
        url = getpass.getpass(
            "Paste your Canvas calendar feed URL (hidden), then press Enter: "
        ).strip()
        register_secret(url)
        try:
            texts.append(fetch_ics(make_session(), url))
        except FeedError as exc:
            raise SystemExit(f"{exc}\nCheck you copied the whole link.") from None
        print("Feed downloaded (kept in memory only).\n")

    def feeds() -> Iterator[str]:
        for i in range(runs):
            yield texts[min(i, len(texts) - 1)]

    now = datetime.fromisoformat(args.now) if args.now else datetime.now(UTC)

    if args.dry_run:
        if not texts:
            parser.error("--dry-run needs --ics or --ask-url")
        context = dry_run_deps(feeds())
    else:
        if args.create_table:
            import boto3

            endpoint = os.environ.get("DYNAMODB_ENDPOINT_URL") or None
            create_table(
                boto3.resource("dynamodb", endpoint_url=endpoint), os.environ["TABLE_NAME"]
            )
        deps = build_deps()
        if texts:
            stream = feeds()
            deps.fetch_feed = lambda: next(stream)
        context = contextlib.nullcontext(deps)

    with context as deps:
        for i in range(runs):
            moment = now + timedelta(minutes=30 * i)
            print(f"Run {i + 1} at {format_local(moment)}")
            print(f"  summary: {run(deps, moment)}")


if __name__ == "__main__":
    main()

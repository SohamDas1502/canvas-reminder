"""Scheduled work for the web app.

Every 30 minutes the dispatcher puts one SQS message per active user on a queue; the
worker runs the shared, tested pipeline for that one user. Users are processed in
parallel and in isolation: one user's broken feed never delays or breaks another's.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from typing import Any

import boto3
import requests

from ..http_client import make_session
from ..ics import FeedError, fetch_ics
from ..messages import digest_message
from ..notifiers import Notifier, PushNotifier
from ..notifiers.email import EmailNotifier
from ..notifiers.ntfy import NtfyNotifier
from ..pipeline import Deps, PipelineError, run
from ..redact import redact, setup_logging
from ..schedule import LOCAL_TZ
from ..state import StateRepository
from .feeds import FeedVault
from .users import UserSettings, UserStore

logger = logging.getLogger(__name__)
DIGEST_HOUR = 8  # local time
DIGEST_DAYS = {"daily": 2, "weekly": 7}


@dataclass
class WebContext:
    users: UserStore
    assignments: Any  # DynamoDB Table keyed by (user, uid)
    session: requests.Session
    ses: Any
    sqs: Any
    queue_url: str
    from_email: str
    ntfy_server: str
    app_url: str

    def repo(self, user_id: str) -> StateRepository:
        return StateRepository(self.assignments, user=user_id)

    def notifiers(self, settings: UserSettings) -> list[Notifier]:
        out: list[Notifier] = []
        if "ntfy" in settings.channels and settings.ntfy_topic:
            out.append(NtfyNotifier(self.session, self.ntfy_server, settings.ntfy_topic))
        if "email" in settings.channels and settings.email:
            out.append(EmailNotifier(self.ses, self.from_email, settings.email, self.app_url))
        return out


_context: WebContext | None = None


def context() -> WebContext:
    """Built once per Lambda container from environment variables set by the template."""
    global _context
    if _context is None:
        dynamodb = boto3.resource("dynamodb")
        _context = WebContext(
            users=UserStore(
                dynamodb.Table(os.environ["USERS_TABLE"]),
                FeedVault(boto3.client("ssm"), os.environ["FEED_PARAMETER_PATH"]),
            ),
            assignments=dynamodb.Table(os.environ["ASSIGNMENTS_TABLE"]),
            session=make_session(),
            ses=boto3.client("sesv2"),
            sqs=boto3.client("sqs"),
            queue_url=os.environ.get("WORK_QUEUE_URL", ""),
            from_email=os.environ.get("FROM_EMAIL", ""),
            ntfy_server=os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
            app_url=_app_url(),
        )
    return _context


def _app_url() -> str:
    """The site's public URL, published by the stack as an SSM parameter."""
    name = os.environ.get("APP_URL_PARAMETER")
    if not name:
        return os.environ.get("APP_URL", "")
    try:
        return str(boto3.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]).rstrip("/")
    except Exception as exc:  # links in emails are a nicety; never block reminders on them
        logger.warning("Could not read the app URL: %s", type(exc).__name__)
        return ""


# --- dispatcher ---------------------------------------------------------------------------


def dispatch_handler(event: dict[str, Any], _: Any) -> dict[str, int]:
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    return {"dispatched": dispatch(context())}


def dispatch(ctx: WebContext) -> int:
    count = 0
    for chunk in _chunks(ctx.users.active_user_ids(), 10):  # SQS batch limit
        ctx.sqs.send_message_batch(
            QueueUrl=ctx.queue_url,
            Entries=[
                {"Id": str(i), "MessageBody": json.dumps({"user": user_id})}
                for i, user_id in enumerate(chunk)
            ],
        )
        count += len(chunk)
    logger.info("Dispatched %d users", count)
    return count


# --- worker -------------------------------------------------------------------------------


def worker_handler(event: dict[str, Any], _: Any) -> dict[str, list[dict[str, str]]]:
    """SQS batch handler. Only unexpected failures are retried (and end up in the DLQ);
    a user's own problems, like a dead feed link, are recorded for their dashboard."""
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    failures = []
    for record in event.get("Records", []):
        try:
            run_for_user(context(), json.loads(record["body"])["user"], datetime.now(UTC))
        except Exception:
            logger.exception("Worker failed for message %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def run_for_user(ctx: WebContext, user_id: str, now: datetime) -> dict[str, int]:
    settings = ctx.users.get(user_id)
    if settings is None or settings.status != "active" or not settings.has_feed:
        return {}
    deps = Deps(
        config=settings.to_config(ctx.ntfy_server),
        repo=ctx.repo(user_id),
        notifiers=ctx.notifiers(settings),
        fetch_feed=lambda: fetch_ics(ctx.session, ctx.users.feed_url(user_id)),
    )
    summary: dict[str, int] = {}
    error = None
    try:
        summary = run(deps, now)
    except (FeedError, PipelineError) as exc:
        error = redact(str(exc))
        logger.warning("Run for %s finished with a problem: %s", user_id, error)
    ctx.users.record_run(user_id, now, error)
    send_digest_if_due(ctx, settings, deps, now)
    return summary


def send_digest_if_due(ctx: WebContext, settings: UserSettings, deps: Deps, now: datetime) -> bool:
    """At 8am Melbourne time: daily, or Mondays for weekly. Claimed first, so at most once."""
    local = now.astimezone(LOCAL_TZ)
    if settings.digest not in DIGEST_DAYS or local.hour != DIGEST_HOUR:
        return False
    if settings.digest == "weekly":
        if local.weekday() != 0:
            return False
        period = f"weekly:{local:%G-W%V}"
    else:
        period = f"daily:{local:%Y-%m-%d}"
    message = digest_message(
        list(deps.repo.load_all().values()), now, DIGEST_DAYS[settings.digest], settings.digest
    )
    if message is None or not ctx.users.claim_once(settings.user_id, "last_digest", period):
        return False
    for notifier in deps.notifiers:
        if isinstance(notifier, PushNotifier):
            try:
                notifier.send(message)
            except Exception as exc:
                logger.warning("Digest via %s failed: %s", notifier.name, type(exc).__name__)
    return True


def _chunks(items: Iterable[str], size: int) -> Iterator[list[str]]:
    iterator = iter(items)
    while chunk := list(islice(iterator, size)):
        yield chunk

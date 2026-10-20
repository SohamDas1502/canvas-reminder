"""One run: fetch -> parse -> diff -> record -> reconcile.

*Record* makes DynamoDB match the feed (versioned, conditional writes).
*Reconcile* then looks only at DynamoDB and makes every channel match it.

Because reconciling is driven by stored state rather than by what changed in *this*
run, a retried run picks up exactly where a failed one stopped. Claims (push) and
leases (reminders) stop two runs from doing the same piece of work.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from botocore.exceptions import ClientError

from .config import Config
from .diff import Change, ChangeKind, diff_assignments, looks_truncated
from .http_client import HttpError
from .ics import FeedError, parse_assignments
from .messages import bootstrap_message, due_soon_message, event_message, removed_message
from .models import Message, Status, StoredAssignment
from .notifiers import Notifier, PushNotifier, ReminderNotifier
from .schedule import due_leads, next_lead
from .state import StateRepository

logger = logging.getLogger(__name__)


class PipelineError(Exception):
    """Some channel operations failed. Raised after all other work is done, so the
    Lambda reports an error (alarm + async retry) without blocking healthy channels."""


@dataclass
class Deps:
    config: Config
    repo: StateRepository
    notifiers: Sequence[Notifier]
    fetch_feed: Callable[[], str]
    setup_errors: list[str] = field(default_factory=list)  # channels that failed to build


def event_key(version: int) -> str:
    return f"event#v{version}"


def removed_key(version: int) -> str:
    return f"removed#v{version}"


def due_key(record: StoredAssignment, lead_seconds: int) -> str:
    # Keyed on the due time, not the content hash: a title edit must not repeat "due in 2h".
    return f"due#{record.due.isoformat()}#{lead_seconds}"


def run(deps: Deps, now: datetime) -> dict[str, int]:
    config, repo = deps.config, deps.repo
    parsed = parse_assignments(deps.fetch_feed())
    records = repo.load_all()
    tracked = {uid: r for uid, r in records.items() if not config.is_ignored(r.course)}
    if looks_truncated(parsed, tracked, now):
        raise FeedError("Feed has no assignments but upcoming ones are on record; skipping run")

    current = [a for a in parsed if a.due > now and not config.is_ignored(a.course)]
    changes = diff_assignments(current, tracked, now)

    # First run: announce everything in one summary instead of one push per assignment.
    first_run = not records
    pushers = [n for n in deps.notifiers if isinstance(n, PushNotifier)]
    quiet = {f"{n.name}:{event_key(1)}" for n in pushers} if first_run else set()
    for change in changes:
        _record(repo, change, now, quiet)

    reconciler = Reconciler(deps, now)
    stored = repo.load_all()
    if first_run:  # the summary goes first, before any urgent due-soon pushes
        new_uids = {c.uid for c in changes if c.kind is ChangeKind.NEW}
        reconciler.bootstrap([stored[uid] for uid in new_uids if uid in stored])
    for record in stored.values():
        reconciler.reconcile(record)

    summary = {"assignments": len(current), **Counter(c.kind.value for c in changes)}
    summary |= reconciler.counts
    logger.info("Run summary: %s", summary)
    failures = deps.setup_errors + reconciler.errors
    if failures:
        raise PipelineError(f"{len(failures)} channel problem(s): " + "; ".join(failures))
    return summary


def _record(repo: StateRepository, change: Change, now: datetime, quiet: set[str]) -> None:
    """Apply one diff entry. A lost race (False) is fine: another run recorded it."""
    current, previous = change.current, change.previous
    if change.kind is ChangeKind.NEW and current:
        repo.create(current, now, sent=quiet)
    elif change.kind is ChangeKind.CHANGED and current and previous:
        repo.apply_change(current, previous.version, previous.due, now)
    elif change.kind is ChangeKind.UNCHANGED:
        repo.touch(change.uid, now)
    elif change.kind is ChangeKind.REMOVED and previous:
        repo.mark_removed(change.uid, previous.version, now)


class Reconciler:
    def __init__(self, deps: Deps, now: datetime) -> None:
        self._repo = deps.repo
        self._config = deps.config
        self._now = now
        self._pushers = [n for n in deps.notifiers if isinstance(n, PushNotifier)]
        self._reminders = [n for n in deps.notifiers if isinstance(n, ReminderNotifier)]
        self.counts: Counter[str] = Counter()
        self.errors: list[str] = []

    def reconcile(self, record: StoredAssignment) -> None:
        if record.due <= self._now or self._config.is_ignored(record.course):
            return
        if record.status is Status.REMOVED:
            self._reconcile_removed(record)
            return

        leads = self._config.leads_for(record.course)
        passed = [
            due_key(record, int(lead.total_seconds()))
            for lead in due_leads(record.due, leads, self._now)
        ]
        for pusher in self._pushers:
            event = self._unsent(pusher, record, [event_key(record.version)])
            reminders = self._unsent(pusher, record, passed)
            if event:
                # The event message states the due date, so it also covers any reminder
                # that is already due; claim those too rather than pushing twice.
                self._push(pusher, record.uid, event + reminders, event_message(record, self._now))
            elif reminders:
                self._push(pusher, record.uid, reminders, due_soon_message(record, self._now))

        for reminder in self._reminders:
            self._sync(reminder, record)

    def bootstrap(self, records: Sequence[StoredAssignment]) -> None:
        if not records:
            return
        anchor = min(records, key=lambda r: (r.due, r.uid)).uid
        for pusher in self._pushers:
            self._push(
                pusher, anchor, [f"{pusher.name}:bootstrap"], bootstrap_message(records, self._now)
            )

    # --- push channels -----------------------------------------------------------------

    def _push(self, pusher: PushNotifier, uid: str, keys: list[str], message: Message) -> None:
        if not self._repo.claim(uid, keys):
            return  # another run already sent it
        try:
            pusher.send(message)
        except Exception as exc:
            if _definitely_not_delivered(exc):
                self._repo.release(uid, keys)  # safe to retry next run
            # Otherwise (timeout, connection reset after sending) the message may have
            # arrived. Keep the claim: a missed push beats a duplicate one.
            self._fail(pusher.name, uid, exc)
            return
        self.counts[f"{pusher.name}_sent"] += 1

    def _reconcile_removed(self, record: StoredAssignment) -> None:
        if self._config.on_removed == "ignore":
            return
        for pusher in self._pushers:
            keys = self._unsent(pusher, record, [removed_key(record.version)])
            if keys:
                self._push(pusher, record.uid, keys, removed_message(record))
        if self._config.on_removed == "delete":
            for reminder in self._reminders:
                ref = record.refs.get(reminder.name)
                if ref is None:
                    continue
                try:
                    reminder.delete(ref["ref"])
                    self._repo.drop_ref(record.uid, reminder.name)
                    self.counts[f"{reminder.name}_deleted"] += 1
                except Exception as exc:
                    self._fail(reminder.name, record.uid, exc)

    @staticmethod
    def _unsent(pusher: PushNotifier, record: StoredAssignment, keys: list[str]) -> list[str]:
        scoped = [f"{pusher.name}:{key}" for key in keys]
        return [key for key in scoped if key not in record.sent]

    # --- reminder channels -------------------------------------------------------------

    def _sync(self, reminder: ReminderNotifier, record: StoredAssignment) -> None:
        if self._is_synced(reminder, record):
            return
        lease = self._repo.acquire_lease(record.uid, reminder.name, self._now)
        if lease is None:
            return  # another run is syncing this one right now
        try:
            fresh = self._repo.get(record.uid) or record  # re-check under the lease
            if self._is_synced(reminder, fresh):
                self._repo.release_lease(record.uid, reminder.name)
                return
            ref = fresh.refs.get(reminder.name, {}).get("ref")
            leads = self._leads(reminder, fresh)
            new_ref = reminder.upsert(fresh, leads, ref)
            fingerprint = sync_fingerprint(fresh, leads)
            self._repo.commit_ref(record.uid, reminder.name, new_ref, fingerprint, lease)
            self.counts[f"{reminder.name}_synced"] += 1
        except Exception as exc:
            self._repo.release_lease(record.uid, reminder.name)
            self._fail(reminder.name, record.uid, exc)

    def _leads(self, reminder: ReminderNotifier, record: StoredAssignment) -> tuple[timedelta, ...]:
        leads = self._config.leads_for(record.course)
        if reminder.single_alert:
            return (next_lead(record.due, leads, self._now),)
        return leads

    def _is_synced(self, reminder: ReminderNotifier, record: StoredAssignment) -> bool:
        stored = record.refs.get(reminder.name, {}).get("hash")
        return stored == sync_fingerprint(record, self._leads(reminder, record))

    def _fail(self, channel: str, uid: str, exc: Exception) -> None:
        message = f"{channel} failed for {uid}: {type(exc).__name__}: {exc}"
        logger.error(message)
        self.errors.append(message)


def sync_fingerprint(record: StoredAssignment, leads: Sequence[timedelta]) -> str:
    """What a reminder was last synced with: the assignment's content *and* its lead times,
    so changing lead times in config (or a single-alert reminder moving on) updates it."""
    seconds = ",".join(str(int(lead.total_seconds())) for lead in leads)
    return hashlib.sha256(f"{record.content_hash}|{seconds}".encode()).hexdigest()


def _definitely_not_delivered(exc: Exception) -> bool:
    """An error *response* (HTTP status, or an AWS API error such as SES rejecting an email)
    means the message was refused, so it is safe to retry. Timeouts are ambiguous."""
    if isinstance(exc, ClientError):
        return True
    return isinstance(exc, HttpError) and exc.status is not None

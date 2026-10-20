"""DynamoDB state: one item per Canvas assignment UID.

Every write that guards a side effect is conditional. A retried or concurrent run can
read the same item, but only one of them can win the write that permits the action:

* `create` / `apply_change` / `mark_removed`: version checks (optimistic locking).
* `claim`: atomically adds message keys to the `sent` set, failing if any is present.
* `acquire_lease`: a short exclusive lease per reminder channel while it is synced.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError

from .models import Assignment, Status, StoredAssignment

TTL_AFTER_DUE = timedelta(days=30)
LEASE_DURATION = timedelta(minutes=5)
DESCRIPTION_LIMIT = 2000


class StateRepository:
    def __init__(self, table: Any, user: str | None = None) -> None:
        """`table` is a boto3 DynamoDB Table resource.

        Single-user tables are keyed by `uid`. In the multi-user web app the table is keyed
        by (`user`, `uid`) and every read and write is scoped to `user`, so one user's
        pipeline can never see or touch another user's assignments.
        """
        self._table = table
        self._user = user

    def _key(self, uid: str) -> dict[str, str]:
        return {"uid": uid} if self._user is None else {"user": self._user, "uid": uid}

    # --- reads -------------------------------------------------------------------------

    def load_all(self) -> dict[str, StoredAssignment]:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"ConsistentRead": True}
        if self._user is not None:
            kwargs["KeyConditionExpression"] = "#user = :user"
            kwargs["ExpressionAttributeNames"] = {"#user": "user"}
            kwargs["ExpressionAttributeValues"] = {":user": self._user}
        read = self._table.scan if self._user is None else self._table.query
        while True:
            page = read(**kwargs)
            items.extend(page["Items"])
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return {item["uid"]: _from_item(item) for item in items}

    def get(self, uid: str) -> StoredAssignment | None:
        item = self._table.get_item(Key=self._key(uid), ConsistentRead=True).get("Item")
        return _from_item(item) if item else None

    # --- recording the feed ------------------------------------------------------------

    def create(self, a: Assignment, now: datetime, sent: Collection[str] = ()) -> bool:
        """Insert a new assignment at version 1. False if it already exists."""
        item = {
            **self._key(a.uid),
            **_assignment_fields(a),
            "version": 1,
            "status": Status.ACTIVE.value,
            "last_seen": now.isoformat(),
            "refs": {},
        }
        if sent:
            item["sent"] = set(sent)
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(#uid)",
                ExpressionAttributeNames={"#uid": "uid"},
            )
        except ClientError as exc:
            if _is_condition_failure(exc):
                return False
            raise
        return True

    def apply_change(
        self, a: Assignment, expected_version: int, previous_due: datetime, now: datetime
    ) -> bool:
        """Store the new content and bump the version. False if someone else got there first."""
        fields = {
            **_assignment_fields(a),
            "previous_due": previous_due.isoformat(),
            "status": Status.ACTIVE.value,
            "last_seen": now.isoformat(),
        }
        return self._set_and_bump(a.uid, fields, expected_version)

    def mark_removed(self, uid: str, expected_version: int, now: datetime) -> bool:
        fields = {"status": Status.REMOVED.value, "removed_at": now.isoformat()}
        return self._set_and_bump(uid, fields, expected_version)

    def touch(self, uid: str, now: datetime) -> None:
        self._update(
            uid,
            "SET #last_seen = :now",
            "attribute_exists(#uid)",
            {"#last_seen": "last_seen", "#uid": "uid"},
            {":now": now.isoformat()},
        )

    # --- idempotency guards ------------------------------------------------------------

    def claim(self, uid: str, keys: Collection[str]) -> bool:
        """Atomically mark message keys as sent. False if any of them already was."""
        not_sent = " AND ".join(f"NOT contains(#sent, :k{i})" for i in range(len(keys)))
        values: dict[str, Any] = {f":k{i}": key for i, key in enumerate(keys)}
        return self._update(
            uid,
            "ADD #sent :keys",
            f"attribute_exists(#uid) AND {not_sent}",
            {"#sent": "sent", "#uid": "uid"},
            values | {":keys": set(keys)},
        )

    def release(self, uid: str, keys: Collection[str]) -> None:
        """Undo a claim whose message definitely was not delivered."""
        self._update(uid, "DELETE #sent :keys", None, {"#sent": "sent"}, {":keys": set(keys)})

    def acquire_lease(self, uid: str, channel: str, now: datetime) -> int | None:
        """Exclusive right to sync this assignment to `channel` for a few minutes."""
        until = int((now + LEASE_DURATION).timestamp())
        acquired = self._update(
            uid,
            "SET #lease = :until",
            "attribute_exists(#uid) AND (attribute_not_exists(#lease) OR #lease < :now)",
            {"#lease": f"lease_{channel}", "#uid": "uid"},
            {":until": until, ":now": int(now.timestamp())},
        )
        return until if acquired else None

    def release_lease(self, uid: str, channel: str) -> None:
        self._update(uid, "REMOVE #lease", None, {"#lease": f"lease_{channel}"}, None)

    def commit_ref(self, uid: str, channel: str, ref: str, content_hash: str, lease: int) -> bool:
        """Record the reminder's id in `channel` and release the lease we hold."""
        return self._update(
            uid,
            "SET #refs.#channel = :ref REMOVE #lease",
            "#lease = :lease",
            {"#refs": "refs", "#channel": channel, "#lease": f"lease_{channel}"},
            {":ref": {"ref": ref, "hash": content_hash}, ":lease": lease},
        )

    def drop_ref(self, uid: str, channel: str) -> None:
        self._update(uid, "REMOVE #refs.#channel", None, {"#refs": "refs", "#channel": channel})

    # --- helpers -----------------------------------------------------------------------

    def _set_and_bump(self, uid: str, fields: dict[str, Any], expected_version: int) -> bool:
        assignments = ", ".join(f"#{k} = :{k}" for k in fields)
        return self._update(
            uid,
            f"SET {assignments}, #version = #version + :one",
            "#version = :expected",
            {f"#{k}": k for k in fields} | {"#version": "version"},
            {f":{k}": v for k, v in fields.items()} | {":one": 1, ":expected": expected_version},
        )

    def _update(
        self,
        uid: str,
        expression: str,
        condition: str | None,
        names: dict[str, str],
        values: dict[str, Any] | None = None,
    ) -> bool:
        kwargs: dict[str, Any] = {
            "Key": self._key(uid),
            "UpdateExpression": expression,
            "ExpressionAttributeNames": names,
        }
        if condition:
            kwargs["ConditionExpression"] = condition
        if values:
            kwargs["ExpressionAttributeValues"] = values
        try:
            self._table.update_item(**kwargs)
        except ClientError as exc:
            if _is_condition_failure(exc):
                return False
            raise
        return True


def _assignment_fields(a: Assignment) -> dict[str, Any]:
    return {
        "title": a.title,
        "course": a.course,
        "due": a.due.isoformat(),
        "description": a.description[:DESCRIPTION_LIMIT],
        "url": a.url,
        "content_hash": a.content_hash,
        "ttl": int((a.due + TTL_AFTER_DUE).timestamp()),
    }


def _from_item(item: dict[str, Any]) -> StoredAssignment:
    previous_due = item.get("previous_due")
    return StoredAssignment(
        uid=item["uid"],
        title=item["title"],
        course=item["course"],
        due=datetime.fromisoformat(item["due"]),
        content_hash=item["content_hash"],
        version=int(item["version"]),
        status=Status(item["status"]),
        description=item.get("description") or "",
        url=item.get("url"),
        previous_due=datetime.fromisoformat(previous_due) if previous_due else None,
        sent=frozenset(item.get("sent", ())),
        refs={k: dict(v) for k, v in item.get("refs", {}).items()},
    )


def _is_condition_failure(exc: ClientError) -> bool:
    return bool(exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException")

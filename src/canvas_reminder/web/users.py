"""Per-user settings for the web app, in DynamoDB (one item per user, keyed by Cognito `sub`).

The Canvas feed link is not in this table at all: it lives in its own encrypted SSM
parameter (see feeds.py); the table only records that one exists. The Apple Reminders token
is stored only as a SHA-256 hash: the user sees it once, and we can verify it but never
show it again.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError

from ..config import Config, ConfigError, config_from_dict, format_duration, parse_leads
from .feeds import FeedVault

# Channels the web app offers in this phase. Google Calendar and To Do come later.
WEB_CHANNELS = ("email", "ntfy", "apple")
DIGESTS = ("off", "daily", "weekly")
DEFAULT_LEADS = ("2d", "2h")


@dataclass(frozen=True)
class UserSettings:
    user_id: str
    email: str
    status: str = "active"
    channels: tuple[str, ...] = ("email",)
    lead_times: tuple[str, ...] = DEFAULT_LEADS
    digest: str = "off"
    ntfy_topic: str = ""
    has_feed: bool = False
    has_apple_token: bool = False
    last_run_at: str | None = None
    last_error: str | None = None

    def to_config(self, ntfy_server: str) -> Config:
        return config_from_dict(
            {
                "channels": list(self.channels),
                "lead_times": list(self.lead_times),
                "ntfy_server": ntfy_server,
            }
        )

    def public(self) -> dict[str, Any]:
        """What the signed-in user may see about themselves (never the feed link)."""
        return {
            "email": self.email,
            "status": self.status,
            "channels": list(self.channels),
            "lead_times": list(self.lead_times),
            "digest": self.digest,
            "ntfy_topic": self.ntfy_topic,
            "feed_connected": self.has_feed,
            "apple_token_set": self.has_apple_token,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
        }


class UserStore:
    def __init__(self, table: Any, feeds: FeedVault) -> None:
        self._table = table
        self._feeds = feeds

    def get(self, user_id: str) -> UserSettings | None:
        item = self._item(user_id)
        return _settings(item) if item else None

    def ensure(self, user_id: str, email: str, now: datetime) -> UserSettings:
        """Create the user on first sign-in; otherwise return what's stored."""
        item = {
            "user_id": user_id,
            "email": email,
            "status": "active",
            "channels": ["email"],
            "lead_times": list(DEFAULT_LEADS),
            "digest": "off",
            "ntfy_topic": "canvas-" + secrets.token_urlsafe(18),
            "created_at": now.isoformat(),
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(user_id)",
            )
        except ClientError as exc:
            if _code(exc) != "ConditionalCheckFailedException":
                raise
            return self._require(user_id)
        return _settings(item)

    def update_preferences(
        self,
        user_id: str,
        *,
        channels: Sequence[str],
        lead_times: Sequence[str],
        digest: str,
        paused: bool,
        now: datetime,
    ) -> UserSettings:
        """Validate and save. Raises ConfigError with a message fit to show the user."""
        chosen = [c for c in WEB_CHANNELS if c in channels]
        unknown = set(channels) - set(WEB_CHANNELS)
        if unknown:
            raise ConfigError(f"Unknown reminder channel: {', '.join(sorted(unknown))}")
        if not chosen:
            raise ConfigError("Pick at least one way to be reminded")
        if digest not in DIGESTS:
            raise ConfigError("Summary must be off, daily or weekly")
        leads = [format_duration(x) for x in parse_leads([str(v) for v in lead_times])]
        self._table.update_item(
            Key={"user_id": user_id},
            UpdateExpression=(
                "SET channels = :c, lead_times = :l, digest = :d, #s = :s, updated_at = :now"
            ),
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":c": chosen,
                ":l": leads,
                ":d": digest,
                ":s": "paused" if paused else "active",
                ":now": now.isoformat(),
            },
        )
        return self._require(user_id)

    # --- the Canvas feed link (its own encrypted parameter) --------------------------------

    def set_feed(self, user_id: str, url: str, now: datetime) -> None:
        if self.get(user_id) is None:
            raise LookupError(f"Unknown user {user_id}")
        self._feeds.put(user_id, url)
        self._table.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET feed_set = :t, updated_at = :now REMOVE last_error",
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeValues={":t": True, ":now": now.isoformat()},
        )

    def feed_url(self, user_id: str) -> str:
        return self._feeds.get(user_id)

    # --- Apple Reminders token (hashed) ---------------------------------------------------

    def new_apple_token(self, user_id: str) -> str:
        """A fresh token, returned once. Replaces (and so revokes) any previous token."""
        secret = secrets.token_urlsafe(32)
        self._table.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET apple_token_hash = :h",
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeValues={":h": _hash(secret)},
        )
        return f"{user_id}.{secret}"

    def user_for_apple_token(self, token: str) -> str | None:
        user_id, _, secret = token.partition(".")
        if not user_id or not secret:
            return None
        item = self._item(user_id)
        stored = (item or {}).get("apple_token_hash", "")
        return user_id if stored and hmac.compare_digest(stored, _hash(secret)) else None

    # --- bookkeeping for the scheduled workers --------------------------------------------

    def active_user_ids(self) -> Iterator[str]:
        """Users who are not paused and have connected a feed."""
        kwargs: dict[str, Any] = {
            "ProjectionExpression": "user_id",
            "FilterExpression": "#s = :active AND attribute_exists(feed_set)",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":active": "active"},
        }
        while True:
            page = self._table.scan(**kwargs)
            yield from (item["user_id"] for item in page["Items"])
            if "LastEvaluatedKey" not in page:
                return
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def record_run(self, user_id: str, now: datetime, error: str | None) -> None:
        expression = "SET last_run_at = :now" + (
            ", last_error = :e" if error else " REMOVE last_error"
        )
        values: dict[str, Any] = {":now": now.isoformat()}
        if error:
            values[":e"] = error[:500]
        self._update_if_exists(user_id, expression, values)

    def claim_once(self, user_id: str, field: str, value: str) -> bool:
        """Set `field` to `value` unless it already is: a once-per-period guard (digests,
        test messages). False if this period was already claimed."""
        try:
            self._table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET #f = :v",
                ConditionExpression=(
                    "attribute_exists(user_id) AND (attribute_not_exists(#f) OR #f <> :v)"
                ),
                ExpressionAttributeNames={"#f": field},
                ExpressionAttributeValues={":v": value},
            )
        except ClientError as exc:
            if _code(exc) == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def delete(self, user_id: str, assignments_table: Any) -> int:
        """Delete the user and every assignment stored for them. Returns items deleted."""
        deleted = 0
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "#u = :u",
            "ExpressionAttributeNames": {"#u": "user"},
            "ExpressionAttributeValues": {":u": user_id},
            "ProjectionExpression": "#u, uid",
        }
        with assignments_table.batch_writer() as batch:
            while True:
                page = assignments_table.query(**kwargs)
                for item in page["Items"]:
                    batch.delete_item(Key={"user": item["user"], "uid": item["uid"]})
                    deleted += 1
                if "LastEvaluatedKey" not in page:
                    break
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        self._feeds.delete(user_id)
        self._table.delete_item(Key={"user_id": user_id})
        return deleted + 1

    # --- helpers --------------------------------------------------------------------------

    def _require(self, user_id: str) -> UserSettings:
        settings = self.get(user_id)
        if settings is None:
            raise LookupError(f"User {user_id} disappeared mid-request")
        return settings

    def _item(self, user_id: str) -> dict[str, Any] | None:
        item: dict[str, Any] | None = self._table.get_item(
            Key={"user_id": user_id}, ConsistentRead=True
        ).get("Item")
        return item

    def _update_if_exists(self, user_id: str, expression: str, values: dict[str, Any]) -> None:
        try:
            self._table.update_item(
                Key={"user_id": user_id},
                UpdateExpression=expression,
                ConditionExpression="attribute_exists(user_id)",
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if _code(exc) != "ConditionalCheckFailedException":
                raise


def _settings(item: dict[str, Any]) -> UserSettings:
    return UserSettings(
        user_id=item["user_id"],
        email=item.get("email", ""),
        status=item.get("status", "active"),
        channels=tuple(item.get("channels", ["email"])),
        lead_times=tuple(item.get("lead_times", DEFAULT_LEADS)),
        digest=item.get("digest", "off"),
        ntfy_topic=item.get("ntfy_topic", ""),
        has_feed=bool(item.get("feed_set")),
        has_apple_token="apple_token_hash" in item,
        last_run_at=item.get("last_run_at"),
        last_error=item.get("last_error"),
    )


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


__all__ = ["DIGESTS", "WEB_CHANNELS", "ConfigError", "UserSettings", "UserStore"]

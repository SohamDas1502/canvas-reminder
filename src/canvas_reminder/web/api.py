"""HTTP API for the web app (API Gateway HTTP API, payload format 2.0).

Every `/api/me/*` route sits behind the Cognito JWT authorizer, and the user id comes
only from the verified token, never from the request body, so a user can only ever
read or change their own data. `/api/apple/feed` is public and checks its own token.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3

from ..apple import build_feed
from ..config import ConfigError
from ..ics import FeedError, fetch_ics, normalize_feed_url, parse_assignments
from ..models import Message
from ..notifiers import PushNotifier
from ..redact import setup_logging
from ..schedule import format_local
from .worker import WebContext, context

logger = logging.getLogger(__name__)
PREVIEW_LIMIT = 10


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Request:
    method: str
    path: str
    body: dict[str, Any]
    headers: dict[str, str]
    claims: dict[str, Any]

    @property
    def user_id(self) -> str:
        sub = self.claims.get("sub")
        if not sub:
            raise ApiError(401, "Please sign in again")
        return str(sub)

    @property
    def email(self) -> str:
        return str(self.claims.get("email", ""))


Handler = Callable[[WebContext, Request, datetime], dict[str, Any]]
ROUTES: dict[str, Handler] = {}


def route(key: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        ROUTES[key] = fn
        return fn

    return register


def lambda_handler(event: dict[str, Any], _: Any) -> dict[str, Any]:
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    handler = ROUTES.get(event.get("routeKey", ""))
    if handler is None:
        return _json(404, {"error": "Not found"})
    try:
        return _json(200, handler(context(), _request(event), datetime.now(UTC)))
    except ApiError as exc:
        return _json(exc.status, {"error": exc.message})
    except Exception:
        logger.exception("Unhandled error on %s", event.get("routeKey"))
        return _json(500, {"error": "Something went wrong on our side. Please try again."})


# --- routes -------------------------------------------------------------------------------


@route("GET /api/me")
def get_me(ctx: WebContext, req: Request, now: datetime) -> dict[str, Any]:
    """Also creates the user record on first sign-in."""
    settings = ctx.users.ensure(req.user_id, req.email, now)
    return {
        **settings.public(),
        "ntfy_server": ctx.ntfy_server,
        "apple_feed_url": f"{ctx.app_url}/api/apple/feed",
    }


@route("PUT /api/me/feed")
def put_feed(ctx: WebContext, req: Request, now: datetime) -> dict[str, Any]:
    """Check the link really is a Canvas calendar, then store it encrypted."""
    ctx.users.ensure(req.user_id, req.email, now)
    try:
        url = normalize_feed_url(str(req.body.get("url", "")))
        assignments = parse_assignments(fetch_ics(ctx.session, url))
    except FeedError:
        raise ApiError(
            400,
            "That doesn't look like a Canvas calendar feed. In Canvas, open Calendar, "
            "click 'Calendar Feed' (bottom right) and copy the whole link.",
        ) from None
    ctx.users.set_feed(req.user_id, url, now)
    upcoming = [a for a in assignments if a.due > now]
    return {
        "upcoming_count": len(upcoming),
        "preview": [
            {"title": a.title, "course": a.course, "due": format_local(a.due)}
            for a in upcoming[:PREVIEW_LIMIT]
        ],
    }


@route("PUT /api/me/preferences")
def put_preferences(ctx: WebContext, req: Request, now: datetime) -> dict[str, Any]:
    body = req.body
    try:
        settings = ctx.users.update_preferences(
            req.user_id,
            channels=[str(c) for c in body.get("channels", [])],
            lead_times=[str(x) for x in body.get("lead_times", [])],
            digest=str(body.get("digest", "off")),
            paused=bool(body.get("paused", False)),
            now=now,
        )
    except ConfigError as exc:
        raise ApiError(400, str(exc)) from None
    return settings.public()


@route("POST /api/me/test")
def send_test(ctx: WebContext, req: Request, now: datetime) -> dict[str, Any]:
    """Send a test reminder to every chosen channel. At most once a minute."""
    settings = ctx.users.get(req.user_id)
    if settings is None:
        raise ApiError(404, "Finish setup first")
    if not ctx.users.claim_once(req.user_id, "last_test", now.strftime("%Y-%m-%dT%H:%M")):
        raise ApiError(429, "You just sent a test. Try again in a minute.")
    message = Message(
        "Test reminder from Canvas Reminders",
        "If you can read this, reminders will reach you here.",
        ("white_check_mark",),
    )
    results: dict[str, str] = {}
    for notifier in ctx.notifiers(settings):
        if isinstance(notifier, PushNotifier):
            try:
                notifier.send(message)
                results[notifier.name] = "sent"
            except Exception as exc:
                logger.warning("Test via %s failed: %s", notifier.name, type(exc).__name__)
                results[notifier.name] = "failed"
    return {"results": results}


@route("POST /api/me/apple-token")
def new_apple_token(ctx: WebContext, req: Request, now: datetime) -> dict[str, Any]:
    ctx.users.ensure(req.user_id, req.email, now)
    return {"token": ctx.users.new_apple_token(req.user_id)}


@route("GET /api/me/assignments")
def list_assignments(ctx: WebContext, req: Request, now: datetime) -> dict[str, Any]:
    records = sorted(ctx.repo(req.user_id).load_all().values(), key=lambda r: r.due)
    return {
        "assignments": [
            {
                "title": r.title,
                "course": r.course,
                "due": format_local(r.due),
                "status": r.status.value,
                "url": r.url,
            }
            for r in records
            if r.due > now
        ]
    }


@route("DELETE /api/me")
def delete_me(ctx: WebContext, req: Request, now: datetime) -> dict[str, Any]:
    """Delete everything we hold about the user, then their sign-in account."""
    deleted = ctx.users.delete(req.user_id, ctx.assignments)
    pool = os.environ.get("USER_POOL_ID")
    if pool:
        username = str(req.claims.get("cognito:username") or req.user_id)
        boto3.client("cognito-idp").admin_delete_user(UserPoolId=pool, Username=username)
    return {"deleted_items": deleted}


@route("GET /api/apple/feed")
def apple_feed(ctx: WebContext, req: Request, now: datetime) -> dict[str, Any]:
    token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
    user_id = ctx.users.user_for_apple_token(token) if token else None
    if user_id is None:
        raise ApiError(401, "Missing or wrong token")
    settings = ctx.users.get(user_id)
    if settings is None or "apple" not in settings.channels or settings.status != "active":
        raise ApiError(404, "Apple Reminders is turned off for this account")
    return build_feed(
        ctx.repo(user_id).load_all().values(), settings.to_config(ctx.ntfy_server), now
    )


# --- helpers ------------------------------------------------------------------------------


def _request(event: dict[str, Any]) -> Request:
    raw = event.get("body") or ""
    if raw and event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        raise ApiError(400, "Request body must be JSON") from None
    context_ = event.get("requestContext", {})
    claims = context_.get("authorizer", {}).get("jwt", {}).get("claims", {})
    return Request(
        method=context_.get("http", {}).get("method", ""),
        path=event.get("rawPath", ""),
        body=body if isinstance(body, dict) else {},
        headers={k.lower(): v for k, v in (event.get("headers") or {}).items()},
        claims=claims,
    )


def _json(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body),
    }

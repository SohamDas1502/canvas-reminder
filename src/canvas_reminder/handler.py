"""AWS Lambda entry point: builds dependencies once per container and runs the pipeline."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import boto3

from .config import load_config
from .http_client import make_session
from .ics import fetch_ics
from .notifiers import build_notifiers
from .pipeline import Deps, run
from .redact import redact, setup_logging
from .secrets import SecretStore
from .state import StateRepository

logger = logging.getLogger(__name__)
_deps: Deps | None = None


class RunFailed(Exception):
    """Sanitised error surfaced to Lambda (its message lands in logs and the DLQ)."""


def build_deps() -> Deps:
    config = load_config()
    secrets = SecretStore(prefix=os.environ.get("SECRET_PREFIX", "canvas-reminder"))
    session = make_session()
    dynamodb = boto3.resource(
        "dynamodb", endpoint_url=os.environ.get("DYNAMODB_ENDPOINT_URL") or None
    )
    setup_errors: list[str] = []
    return Deps(
        config=config,
        repo=StateRepository(dynamodb.Table(os.environ["TABLE_NAME"])),
        notifiers=build_notifiers(config, secrets, session, setup_errors),
        fetch_feed=lambda: fetch_ics(session, secrets.get("ics-url")),
        setup_errors=setup_errors,
    )


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    global _deps
    try:
        if _deps is None:
            _deps = build_deps()
        return run(_deps, datetime.now(UTC))
    except Exception as exc:
        logger.exception("Run failed")
        # Lambda prints uncaught exceptions itself, bypassing our log filter, so only a
        # redacted message (and no chained traceback) is allowed to escape.
        raise RunFailed(redact(f"{type(exc).__name__}: {exc}")) from None

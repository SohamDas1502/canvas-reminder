"""Budget kill switch: if the month's AWS bill passes the budget, stop everything billable.

AWS Budgets publishes to an SNS topic, which invokes this function. It:

1. disables the EventBridge schedule, so no more runs;
2. sets both functions' reserved concurrency to 0, so nothing can invoke them (including
   the public Apple Reminders URL).

Secrets live in Parameter Store's free tier, so they are left alone. What remains (the
table, parameters, logs, queue and topics) costs nothing at rest. `make resume` undoes
both steps at any time.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Any

import boto3

from .config import load_config
from .http_client import make_session
from .models import Message
from .notifiers.ntfy import NtfyNotifier
from .redact import setup_logging
from .secrets import SecretStore

logger = logging.getLogger(__name__)
# Fields of GetSchedule's response that UpdateSchedule accepts (it replaces the whole schedule).
_SCHEDULE_FIELDS = (
    "Name",
    "GroupName",
    "ScheduleExpression",
    "ScheduleExpressionTimezone",
    "FlexibleTimeWindow",
    "Target",
    "Description",
    "StartDate",
    "EndDate",
    "KmsKeyArn",
    "ActionAfterCompletion",
)


def pause(
    scheduler: Any, lambda_client: Any, *, schedule_name: str, functions: Sequence[str]
) -> list[str]:
    """Stop all billable activity. Idempotent: safe to run more than once."""
    done = [_set_schedule(scheduler, schedule_name, "DISABLED")]
    for function in functions:
        lambda_client.put_function_concurrency(
            FunctionName=function, ReservedConcurrentExecutions=0
        )
        done.append(f"function {function} blocked")
    return done


def resume(
    scheduler: Any, lambda_client: Any, *, schedule_name: str, functions: Sequence[str]
) -> list[str]:
    """Undo `pause`: unblock the functions, then re-enable the schedule."""
    done = []
    for function in functions:
        lambda_client.delete_function_concurrency(FunctionName=function)
        done.append(f"function {function} unblocked")
    done.append(_set_schedule(scheduler, schedule_name, "ENABLED"))
    return done


def lambda_handler(event: dict[str, Any], context: Any) -> list[str]:
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    _notify_phone()
    done = pause(
        boto3.client("scheduler"),
        boto3.client("lambda"),
        schedule_name=os.environ["SCHEDULE_NAME"],
        functions=os.environ["FUNCTION_NAMES"].split(","),
    )
    logger.warning("Budget exceeded, pipeline paused: %s", "; ".join(done))
    return done


def _notify_phone() -> None:
    """Best effort: the budget email is the guaranteed notification."""
    try:
        secrets = SecretStore(prefix=os.environ.get("SECRET_PREFIX", "canvas-reminder"))
        ntfy = secrets.get_json("ntfy")
        notifier = NtfyNotifier(make_session(), load_config().ntfy_server, ntfy["topic"])
        notifier.send(
            Message(
                "Canvas reminders paused: AWS budget reached",
                "Everything that costs money has been stopped. Run `make resume` "
                "to turn it back on.",
                ("warning",),
                priority=5,
            )
        )
    except Exception as exc:
        logger.warning("Could not send the pause notification: %s", type(exc).__name__)


def _set_schedule(scheduler: Any, name: str, state: str) -> str:
    current = scheduler.get_schedule(Name=name)
    if current.get("State") != state:
        fields = {key: current[key] for key in _SCHEDULE_FIELDS if key in current}
        scheduler.update_schedule(**fields, State=state)
    return f"schedule {name} {state.lower()}"

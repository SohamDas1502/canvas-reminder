from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from canvas_reminder.models import Assignment, StoredAssignment

FIXTURES = Path(__file__).parent / "fixtures"
# Tuesday 20 Oct 2026, 11:00 am in Melbourne (AEDT, UTC+11).
NOW = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)

# Never let a test talk to real AWS.
os.environ.update(
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
    AWS_SESSION_TOKEN="testing",
    AWS_DEFAULT_REGION="ap-southeast-2",
)


def sample_ics(name: str = "sample.ics") -> str:
    return (FIXTURES / name).read_text()


def make_assignment(uid: str = "event-assignment-1", **overrides: object) -> Assignment:
    base = Assignment(
        uid=uid,
        title="Essay",
        course="ABC10001_2026_SM2",
        due=datetime(2026, 10, 24, 12, 59, tzinfo=UTC),
        description="Write things.",
        url="https://canvas.example.edu/courses/1/assignments/1",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def stored(assignment: Assignment, **overrides: object) -> StoredAssignment:
    record = StoredAssignment(
        uid=assignment.uid,
        title=assignment.title,
        course=assignment.course,
        due=assignment.due,
        content_hash=assignment.content_hash,
        version=1,
        description=assignment.description,
        url=assignment.url,
    )
    return replace(record, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def table():  # type: ignore[no-untyped-def]
    """An empty DynamoDB table with the same key schema as template.yaml, mocked by moto."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        resource = boto3.resource("dynamodb")
        yield resource.create_table(
            TableName="assignments",
            KeySchema=[{"AttributeName": "uid", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "uid", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )


@pytest.fixture
def repo(table):  # type: ignore[no-untyped-def]
    from canvas_reminder.state import StateRepository

    return StateRepository(table)

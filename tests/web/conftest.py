"""A full multi-user environment on moto: users table, assignments table, feed parameters, queue."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from moto import mock_aws

from canvas_reminder.http_client import make_session
from canvas_reminder.web.feeds import FeedVault
from canvas_reminder.web.users import UserStore
from canvas_reminder.web.worker import WebContext


class FakeSes:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = False

    def send_email(self, **kwargs: Any) -> None:
        if self.fail:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "MessageRejected"}}, "SendEmail")
        self.sent.append(kwargs)

    @property
    def subjects(self) -> list[str]:
        return [m["Content"]["Simple"]["Subject"]["Data"] for m in self.sent]

    def to(self, address: str) -> list[str]:
        return [
            m["Content"]["Simple"]["Subject"]["Data"]
            for m in self.sent
            if m["Destination"]["ToAddresses"] == [address]
        ]


@pytest.fixture
def web():  # type: ignore[no-untyped-def]
    with mock_aws():
        dynamodb = boto3.resource("dynamodb")
        users_table = dynamodb.create_table(
            TableName="users",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        assignments = dynamodb.create_table(
            TableName="assignments",
            KeySchema=[
                {"AttributeName": "user", "KeyType": "HASH"},
                {"AttributeName": "uid", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user", "AttributeType": "S"},
                {"AttributeName": "uid", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs = boto3.client("sqs")
        queue_url = sqs.create_queue(QueueName="work")["QueueUrl"]
        yield WebContext(
            users=UserStore(
                users_table, FeedVault(boto3.client("ssm"), "/canvas-reminder-web/feeds")
            ),
            assignments=assignments,
            session=make_session(retries=0),
            ses=FakeSes(),
            sqs=sqs,
            queue_url=queue_url,
            from_email="reminders@example.edu",
            ntfy_server="https://ntfy.sh",
            app_url="https://app.example.edu",
        )

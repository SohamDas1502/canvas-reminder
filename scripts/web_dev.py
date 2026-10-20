"""Run the website locally against the real API code, with no AWS account.

    make web-dev        # then open http://127.0.0.1:8100

AWS is replaced by in-memory moto mocks, sign-in by a fake Cognito that accepts the code
123456 for any email. Paste your real Canvas feed link to try it with your own calendar
(fetched read-only, kept in memory), or any link containing "example" for the committed
sample. Nothing is sent: emails and phone notifications are printed here instead.
"""

from __future__ import annotations

import base64
import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

from canvas_reminder.http_client import make_session  # noqa: E402
from canvas_reminder.ics import fetch_ics as real_fetch_ics  # noqa: E402
from canvas_reminder.models import Message  # noqa: E402
from canvas_reminder.notifiers.ntfy import NtfyNotifier  # noqa: E402
from canvas_reminder.web import api, worker  # noqa: E402
from canvas_reminder.web.feeds import FeedVault  # noqa: E402
from canvas_reminder.web.users import UserStore  # noqa: E402
from canvas_reminder.web.worker import WebContext  # noqa: E402

SITE = ROOT / "web" / "site"
SAMPLE = (ROOT / "tests" / "fixtures" / "sample.ics").read_text()
PORT = 8100
DEV_CODE = "123456"


class PrintingSes:
    def send_email(self, **kwargs: Any) -> None:
        content = kwargs["Content"]["Simple"]
        print(
            f"  [email to {kwargs['Destination']['ToAddresses'][0]}] {content['Subject']['Data']}"
        )


def print_ntfy(self: NtfyNotifier, message: Message) -> None:
    print(f"  [phone notification] {message.title}")


def build_context() -> WebContext:
    dynamodb = boto3.resource("dynamodb", region_name="ap-southeast-2")
    users = dynamodb.create_table(
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
    return WebContext(
        users=UserStore(
            users, FeedVault(boto3.client("ssm", region_name="ap-southeast-2"), "/dev/feeds")
        ),
        assignments=assignments,
        session=make_session(),
        ses=PrintingSes(),
        sqs=None,
        queue_url="",
        from_email="dev@example.edu",
        ntfy_server="https://ntfy.sh",
        app_url=f"http://127.0.0.1:{PORT}",
    )


def preview_fetch(session: Any, url: str) -> str:
    """Links containing "example" get the committed sample; real links are really fetched
    (read-only), so you can try the site with your own calendar. Nothing is stored on disk."""
    if "example" in url:
        return SAMPLE
    return real_fetch_ics(session, url)


def dev_token(email: str) -> str:
    return "dev." + base64.urlsafe_b64encode(email.encode()).decode()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(SITE), **kwargs)

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/config.json":
            self._json(
                200,
                {"region": "local", "userPoolClientId": "dev", "cognitoEndpoint": "/dev-cognito"},
            )
        elif self.path.startswith("/api/"):
            self._api("GET")
        else:
            super().do_GET()

    def do_PUT(self) -> None:
        self._api("PUT")

    def do_DELETE(self) -> None:
        self._api("DELETE")

    def do_POST(self) -> None:
        if self.path == "/dev-cognito":
            self._cognito()
        else:
            self._api("POST")

    # A stand-in for Cognito's passwordless flow: every code is 123456.
    def _cognito(self) -> None:
        action = self.headers["X-Amz-Target"].split(".")[-1]
        body = self._body()
        email = (
            body.get("Username")
            or body.get("AuthParameters", {}).get("USERNAME")
            or body.get("ChallengeResponses", {}).get("USERNAME")
            or ""
        )
        tokens = {
            "AuthenticationResult": {
                "IdToken": dev_token(email),
                "RefreshToken": dev_token(email),
                "ExpiresIn": 86400,
            }
        }
        if action == "SignUp":
            self._json(400, {"__type": "UsernameExistsException"})  # always go straight to a code
        elif action == "InitiateAuth" and body.get("AuthFlow") == "REFRESH_TOKEN_AUTH":
            self._json(
                200,
                {
                    "AuthenticationResult": {
                        "IdToken": body["AuthParameters"]["REFRESH_TOKEN"],
                        "ExpiresIn": 86400,
                    }
                },
            )
        elif action == "InitiateAuth":
            print(f"  [sign-in code for {email}] {DEV_CODE}")
            self._json(200, {"ChallengeName": "EMAIL_OTP", "Session": "dev"})
        elif action == "RespondToAuthChallenge":
            if body.get("ChallengeResponses", {}).get("EMAIL_OTP_CODE") == DEV_CODE:
                self._json(200, tokens)
            else:
                self._json(400, {"__type": "CodeMismatchException"})
        else:
            self._json(400, {"__type": "InvalidParameterException"})

    def _api(self, method: str) -> None:
        auth = self.headers.get("Authorization", "").removeprefix("Bearer ")
        claims = {}
        if auth.startswith("dev."):
            email = base64.urlsafe_b64decode(auth[4:]).decode()
            claims = {"sub": "dev-" + email.replace("@", "-at-"), "email": email}
        path = self.path.split("?")[0]
        event = {
            "routeKey": f"{method} {path}",
            "rawPath": path,
            "body": self._raw_body() or None,
            "headers": dict(self.headers),
            "requestContext": {
                "http": {"method": method},
                "authorizer": {"jwt": {"claims": claims}},
            },
        }
        if path.startswith("/api/me") and not claims:
            self._json(401, {"message": "Unauthorized"})
            return
        response = api.lambda_handler(event, None)
        if method == "PUT" and path == "/api/me/feed" and response["statusCode"] == 200:
            worker.run_for_user(
                CONTEXT, claims["sub"], worker.datetime.now(worker.UTC)
            )  # first check now
        self.send_response(response["statusCode"])
        for key, value in response["headers"].items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(response["body"].encode())

    def _raw_body(self) -> str:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length).decode() if length else ""

    def _body(self) -> dict[str, Any]:
        raw = self._raw_body()
        return json.loads(raw) if raw else {}

    def _json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    with mock_aws():
        CONTEXT = build_context()
        api.context = lambda: CONTEXT  # type: ignore[assignment]
        api.fetch_ics = preview_fetch  # type: ignore[assignment]
        worker.fetch_ics = preview_fetch  # type: ignore[assignment]
        NtfyNotifier.send = print_ntfy  # type: ignore[method-assign]
        print(
            f"Local preview at http://127.0.0.1:{PORT}  (sign-in code: {DEV_CODE}; Ctrl+C to stop)"
        )
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

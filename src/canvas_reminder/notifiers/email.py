"""Email reminders through Amazon SES (web app). Same interface as ntfy: one message, now."""

from __future__ import annotations

from html import escape
from typing import Any

from ..models import Message
from .base import PushNotifier


class EmailNotifier(PushNotifier):
    name = "email"

    def __init__(self, ses: Any, from_address: str, to_address: str, manage_url: str = "") -> None:
        """`ses` is a boto3 `sesv2` client."""
        self._ses = ses
        self._from = from_address
        self._to = to_address
        self._manage_url = manage_url

    def send(self, message: Message) -> None:
        text = message.body
        html = "<br>".join(escape(line) for line in message.body.splitlines())
        if message.click:
            text += f"\n\nOpen in Canvas: {message.click}"
            html += f'<p><a href="{escape(message.click, quote=True)}">Open in Canvas</a></p>'
        if self._manage_url:
            text += f"\n\nChange or stop these reminders: {self._manage_url}"
            link = escape(self._manage_url, quote=True)
            html += (
                '<p style="color:#667085;font-size:13px">'
                f'<a href="{link}">Change or stop these reminders</a></p>'
            )
        self._ses.send_email(
            FromEmailAddress=self._from,
            Destination={"ToAddresses": [self._to]},
            Content={
                "Simple": {
                    "Subject": {"Data": message.title, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": text, "Charset": "UTF-8"},
                        "Html": {"Data": f"<p>{html}</p>", "Charset": "UTF-8"},
                    },
                }
            },
        )

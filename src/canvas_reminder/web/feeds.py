"""Where users' Canvas feed links live: one encrypted SSM parameter per user.

A feed link is a credential for that student's calendar, so it is kept out of the
users table entirely. Each is a Standard-tier `SecureString` parameter encrypted with
the AWS-managed `aws/ssm` key, which is free. Someone who can read the users table,
or a bug that leaks table rows, still never sees a link.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from ..redact import register_secret


class FeedVault:
    def __init__(self, ssm: Any, prefix: str) -> None:
        """`prefix` is a parameter path such as /canvas-reminder-web/feeds."""
        self._ssm = ssm
        self._prefix = prefix.rstrip("/")

    def name(self, user_id: str) -> str:
        return f"{self._prefix}/{user_id}"

    def put(self, user_id: str, url: str) -> None:
        self._ssm.put_parameter(
            Name=self.name(user_id),
            Value=url,
            Type="SecureString",
            Tier="Standard",  # free; Advanced is billed
            Overwrite=True,
        )

    def get(self, user_id: str) -> str:
        try:
            response = self._ssm.get_parameter(Name=self.name(user_id), WithDecryption=True)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
                raise LookupError("No Canvas feed connected") from None
            raise
        url = str(response["Parameter"]["Value"])
        register_secret(url)
        return url

    def delete(self, user_id: str) -> None:
        try:
            self._ssm.delete_parameter(Name=self.name(user_id))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ParameterNotFound":
                raise

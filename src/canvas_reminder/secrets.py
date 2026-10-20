"""Secrets from SSM Parameter Store, or from environment variables for local runs.

Each secret is a Standard-tier `SecureString` parameter, encrypted with the AWS-managed
`aws/ssm` key: free to store and to read at this volume. Parameter
`/canvas-reminder/ics-url` maps to env var `CANVAS_REMINDER_ICS_URL`. Every value loaded
is registered with the log redactor before it is returned.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from typing import Any

import boto3

from .redact import register_secret

logger = logging.getLogger(__name__)


class SecretStore:
    def __init__(
        self,
        prefix: str = "canvas-reminder",
        client: Any = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._prefix = prefix
        self._client = client
        self._env = os.environ if env is None else env
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str:
        if name not in self._cache:
            value = self._env.get(self._env_key(name)) or self._fetch(name)
            _register(value)
            self._cache[name] = value
        return self._cache[name]

    def get_json(self, name: str) -> dict[str, Any]:
        value = json.loads(self.get(name))
        if not isinstance(value, dict):
            raise ValueError(f"Parameter {self.parameter_name(name)} must be a JSON object")
        return value

    def put_json(self, name: str, value: dict[str, Any]) -> None:
        """Write back a secret (used for rotated OAuth refresh tokens)."""
        text = json.dumps(value)
        _register(text)
        self._cache[name] = text
        if self._env.get(self._env_key(name)):
            logger.warning("Secret %s comes from the environment; not writing it back", name)
            return
        self._boto().put_parameter(
            Name=self.parameter_name(name),
            Value=text,
            Type="SecureString",
            Tier="Standard",  # Advanced tier is billed; Standard is free
            Overwrite=True,
        )

    def parameter_name(self, name: str) -> str:
        return f"/{self._prefix}/{name}"

    def _fetch(self, name: str) -> str:
        response = self._boto().get_parameter(Name=self.parameter_name(name), WithDecryption=True)
        return str(response["Parameter"]["Value"])

    def _boto(self) -> Any:
        if self._client is None:
            self._client = boto3.client("ssm")
        return self._client

    def _env_key(self, name: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "_", f"{self._prefix}/{name}").upper()


def _register(value: str) -> None:
    register_secret(value)
    try:
        parsed = json.loads(value)
    except ValueError:
        return
    if isinstance(parsed, dict):
        for item in parsed.values():
            if isinstance(item, str):
                register_secret(item)

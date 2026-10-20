"""Save an OAuth secret to SSM Parameter Store (SecureString) or to .env. Never prints it."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
PREFIX = os.environ.get("SECRET_PREFIX", "canvas-reminder")


def store_secret(name: str, value: dict[str, Any], dotenv: bool = False) -> None:
    text = json.dumps(value)
    if dotenv:
        env_key = f"{PREFIX}_{name}".upper().replace("-", "_")
        path = ROOT / ".env"
        lines = [
            line
            for line in (path.read_text().splitlines() if path.exists() else [])
            if not line.startswith(f"{env_key}=")
        ]
        path.write_text("\n".join([*lines, f"{env_key}={text}"]) + "\n")
        path.chmod(0o600)
        print(f"Saved {env_key} to .env (git-ignored).")
        return

    import boto3

    client = boto3.client("ssm", region_name=REGION)
    parameter = f"/{PREFIX}/{name}"
    client.put_parameter(
        Name=parameter, Value=text, Type="SecureString", Tier="Standard", Overwrite=True
    )
    print(f"Saved {parameter} in {REGION} (encrypted).")

"""One-off: authorise Microsoft To Do access (personal account) and store the refresh token.

Uses the device code flow, so no redirect URI or client secret is needed. The token is
written straight to SSM Parameter Store (or to .env with --dotenv); it is never printed.

  python scripts/ms_auth.py --client-id <Application (client) ID>
"""

from __future__ import annotations

import argparse
import time

import requests
from _store import store_secret

TENANT = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
SCOPES = "Tasks.ReadWrite offline_access"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--dotenv", action="store_true", help="write to .env instead of AWS")
    args = parser.parse_args()

    device = requests.post(
        f"{TENANT}/devicecode", data={"client_id": args.client_id, "scope": SCOPES}, timeout=20
    )
    device.raise_for_status()
    flow = device.json()
    print(flow["message"])  # "To sign in, use a web browser to open ... and enter the code ..."

    deadline = time.monotonic() + int(flow["expires_in"])
    while time.monotonic() < deadline:
        time.sleep(int(flow.get("interval", 5)))
        response = requests.post(
            f"{TENANT}/token",
            data={
                "client_id": args.client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": flow["device_code"],
            },
            timeout=20,
        )
        body = response.json()
        if response.ok:
            value = {"client_id": args.client_id, "refresh_token": body["refresh_token"]}
            store_secret("microsoft", value, dotenv=args.dotenv)
            return
        if body.get("error") not in ("authorization_pending", "slow_down"):
            raise SystemExit(f"Authorisation failed: {body.get('error')}")
    raise SystemExit("Timed out waiting for authorisation")


if __name__ == "__main__":
    main()

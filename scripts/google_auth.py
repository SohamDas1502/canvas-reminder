"""One-off: authorise Google Calendar access and store the refresh token.

Uses the OAuth "installed app" flow with a loopback redirect and PKCE. The token is
written straight to SSM Parameter Store (or to .env with --dotenv); it is never printed.

  python scripts/google_auth.py --client-secret-file ~/Downloads/client_secret_XXX.json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import urllib.parse
import webbrowser
from pathlib import Path

import requests
from _store import store_secret

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
SCOPE = "https://www.googleapis.com/auth/calendar.events"


def wait_for_code(port: int, state: str) -> str:
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if query.get("state") == [state] and "code" in query:
                result["code"] = query["code"][0]
                text = "Done. You can close this tab and return to the terminal."
            else:
                text = "Authorisation failed or was cancelled."
            self.send_response(200)
            self.end_headers()
            self.wfile.write(text.encode())

        def log_message(self, *args: object) -> None:  # keep the code out of the terminal
            pass

    with http.server.HTTPServer(("127.0.0.1", port), Handler) as server:
        while "code" not in result:
            server.handle_request()
    return result["code"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--client-secret-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dotenv", action="store_true", help="write to .env instead of AWS")
    args = parser.parse_args()

    client = json.loads(args.client_secret_file.read_text())["installed"]
    redirect = f"http://127.0.0.1:{args.port}"
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")

    params = {
        "client_id": client["client_id"],
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # always return a refresh token
        "state": state,
        "code_challenge": challenge.decode(),
        "code_challenge_method": "S256",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("Opening your browser to authorise Google Calendar access...")
    webbrowser.open(url)
    code = wait_for_code(args.port, state)

    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        },
        timeout=20,
    )
    response.raise_for_status()
    value = {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": response.json()["refresh_token"],
    }
    store_secret("google", value, dotenv=args.dotenv)


if __name__ == "__main__":
    main()

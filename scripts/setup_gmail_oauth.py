#!/usr/bin/env python3
"""One-time interactive Gmail OAuth setup for JobAgent.

Usage:
    uv run python scripts/setup_gmail_oauth.py

Expects data/gmail_credentials.json (an OAuth "Desktop app" client downloaded
from Google Cloud Console). Opens a browser for consent and saves the token
to data/gmail_token.json (chmod 600).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = ROOT / "data" / "gmail_credentials.json"
TOKEN_PATH = ROOT / "data" / "gmail_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

MISSING_CREDENTIALS_HELP = f"""\
Missing {CREDENTIALS_PATH}

Do this once, then re-run this script:

  1. Go to https://console.cloud.google.com/ and create (or pick) a project.
  2. APIs & Services -> Library -> search "Gmail API" -> Enable.
  3. APIs & Services -> OAuth consent screen:
       - User type: EXTERNAL
       - Fill in app name + your email (minimal fields are fine)
       - IMPORTANT: publish the app to "In production". A consent screen left
         in "Testing" issues refresh tokens that expire every 7 days.
  4. APIs & Services -> Credentials -> Create credentials -> OAuth client ID
       - Application type: Desktop app
  5. Download the client JSON and save it exactly as:
       {CREDENTIALS_PATH}
  6. Re-run:
       uv run python scripts/setup_gmail_oauth.py
"""


def main() -> int:
    if not CREDENTIALS_PATH.exists():
        print(MISSING_CREDENTIALS_HELP)
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    print("Opening a browser window for Google consent...")
    print("Sign in with the Gmail account JobAgent should send from.\n")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)

    TOKEN_PATH.parent.mkdir(exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    os.chmod(TOKEN_PATH, 0o600)

    print(f"\nToken saved to {TOKEN_PATH} (mode 600).")
    print("Gmail is ready. Sanity-check with:")
    print("  uv run jobagent outreach run --shadow")
    print("  uv run jobagent digest")
    return 0


if __name__ == "__main__":
    sys.exit(main())

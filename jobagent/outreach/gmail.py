"""Shared Gmail API client.

Token lives at data/gmail_token.json (created by scripts/setup_gmail_oauth.py).
Auto-refreshes and persists the refreshed token. Every entry point raises
GmailNotConfigured with copy-pasteable setup instructions when the token is
missing, so callers can fail crisply instead of stack-tracing.
"""
from __future__ import annotations

import base64
import mimetypes
import os
from email.message import EmailMessage
from pathlib import Path

from jobagent.db import DATA_DIR

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

CREDENTIALS_PATH = DATA_DIR / "gmail_credentials.json"
TOKEN_PATH = DATA_DIR / "gmail_token.json"

SETUP_INSTRUCTIONS = f"""\
Gmail is not set up yet ({TOKEN_PATH} missing).

One-time setup:
  1. Go to https://console.cloud.google.com/ and create (or pick) a project.
  2. APIs & Services -> Library -> enable "Gmail API".
  3. APIs & Services -> OAuth consent screen -> User type EXTERNAL,
     fill the minimal fields, then PUBLISH the app to "In production"
     (a "Testing" app's refresh tokens expire after 7 days).
  4. APIs & Services -> Credentials -> Create credentials ->
     OAuth client ID -> Application type "Desktop app".
  5. Download the client JSON and save it as:
        {CREDENTIALS_PATH}
  6. Run:
        uv run python scripts/setup_gmail_oauth.py
     A browser window opens; approve access with the Gmail account you
     want JobAgent to send from. The token is saved to
        {TOKEN_PATH}
"""


class GmailNotConfigured(RuntimeError):
    """Raised when data/gmail_token.json does not exist (or is unusable)."""


def _save_token(creds) -> None:
    TOKEN_PATH.parent.mkdir(exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    os.chmod(TOKEN_PATH, 0o600)


def service():
    """Build an authenticated Gmail API client, refreshing the token if needed."""
    if not TOKEN_PATH.exists():
        raise GmailNotConfigured(SETUP_INSTRUCTIONS)

    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    except ValueError as e:
        raise GmailNotConfigured(
            f"{TOKEN_PATH} is malformed ({e}).\n\n{SETUP_INSTRUCTIONS}"
        ) from e

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise GmailNotConfigured(
                    f"Stored Gmail token could not be refreshed ({e}). "
                    f"Delete {TOKEN_PATH} and re-run setup.\n\n{SETUP_INSTRUCTIONS}"
                ) from e
            _save_token(creds)
        else:
            raise GmailNotConfigured(
                f"Stored Gmail token is invalid and has no refresh token. "
                f"Delete {TOKEN_PATH} and re-run setup.\n\n{SETUP_INSTRUCTIONS}"
            )

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_message(
    to: str,
    subject: str,
    body_text: str,
    attachments: list[Path] | None = None,
    thread_id: str | None = None,
    svc=None,
) -> dict:
    """Send a plain-text email (optionally with attachments / into a thread).

    Returns {"id": ..., "threadId": ...} from the Gmail API.
    """
    svc = svc or service()

    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body_text)

    for path in attachments or []:
        path = Path(path)
        if not path.exists():
            continue
        ctype, _ = mimetypes.guess_type(str(path))
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
        )

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    body: dict = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id

    resp = svc.users().messages().send(userId="me", body=body).execute()
    return {"id": resp.get("id"), "threadId": resp.get("threadId")}


def list_threads(query: str, max_pages: int = 3, svc=None) -> list[dict]:
    """List thread stubs ({id, snippet, historyId}) matching a Gmail query."""
    svc = svc or service()
    threads: list[dict] = []
    page_token = None
    for _ in range(max_pages):
        resp = (
            svc.users()
            .threads()
            .list(userId="me", q=query, maxResults=100, pageToken=page_token)
            .execute()
        )
        threads.extend(resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return threads


def get_thread(thread_id: str, svc=None) -> dict:
    """Fetch a full thread (metadata format: From/To/Subject/Date headers + snippets)."""
    svc = svc or service()
    return (
        svc.users()
        .threads()
        .get(
            userId="me",
            id=thread_id,
            format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        )
        .execute()
    )

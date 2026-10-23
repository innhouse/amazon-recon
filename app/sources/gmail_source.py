"""Read Amazon emails from Gmail with OAuth (read-only scope)."""
from __future__ import annotations

import base64
from datetime import date, timedelta

from .. import config, db
from ..parsers.amazon_email import parse_message
from .common import SyncStats, store_result


def _credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if config.GOOGLE_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(config.GOOGLE_TOKEN_PATH), config.GMAIL_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not config.GOOGLE_CLIENT_SECRETS.exists():
            raise FileNotFoundError(
                f"Google OAuth client file not found at {config.GOOGLE_CLIENT_SECRETS}. "
                "See README section 'Gmail setup'."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(config.GOOGLE_CLIENT_SECRETS), config.GMAIL_SCOPES)
        creds = flow.run_local_server(port=0)
        config.GOOGLE_TOKEN_PATH.write_text(creds.to_json())
    return creds


def is_configured() -> bool:
    return config.GOOGLE_TOKEN_PATH.exists() or config.GOOGLE_CLIENT_SECRETS.exists()


def _walk_parts(payload):
    yield payload
    for p in payload.get("parts", []) or []:
        yield from _walk_parts(p)


def _bodies(payload):
    text = html = None
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
        if mime == "text/plain" and text is None:
            text = decoded
        elif mime == "text/html" and html is None:
            html = decoded
    return text, html


def sync(conn, lookback_days=None) -> SyncStats:
    from googleapiclient.discovery import build

    stats = SyncStats()
    service = build("gmail", "v1", credentials=_credentials(), cache_discovery=False)
    last = db.kv_get(conn, "gmail_last_sync_date")
    if last:
        since = date.fromisoformat(last) - timedelta(days=3)
    else:
        since = date.today() - timedelta(days=lookback_days or config.GMAIL_INITIAL_LOOKBACK_DAYS)
    query = f"{config.GMAIL_QUERY} after:{since.strftime('%Y/%m/%d')}"

    page_token = None
    while True:
        resp = service.users().messages().list(userId="me", q=query, pageToken=page_token, maxResults=100).execute()
        for ref in resp.get("messages", []):
            key = f"gmail:{ref['id']}"
            if db.is_processed(conn, key):
                stats.bump("skipped")
                continue
            msg = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            text, html = _bodies(msg["payload"])
            try:
                result = parse_message(subject=headers.get("subject", ""), sender=headers.get("from", ""),
                                       date_header=headers.get("date"), body_text=text, body_html=html,
                                       message_id=key)
                stats.bump(store_result(conn, result, key, "gmail"))
            except Exception as exc:
                db.mark_processed(conn, key, "gmail", "error", str(exc)[:300])
                stats.bump("error")
            conn.commit()
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    db.kv_set(conn, "gmail_last_sync_date", date.today().isoformat())
    conn.commit()
    return stats

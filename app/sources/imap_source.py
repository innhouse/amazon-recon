"""Read Amazon emails from any IMAP mailbox (iCloud, Outlook, Fastmail, Gmail via app password...).

iCloud settings: host imap.mail.me.com, port 993, username = full iCloud address,
password = an app-specific password generated at https://account.apple.com
(Sign-In and Security -> App-Specific Passwords). The normal Apple ID password
will NOT work.
"""
from __future__ import annotations

import imaplib
import os
from datetime import date, timedelta

from .. import db
from ..parsers.amazon_email import parse_eml
from .common import SyncStats, store_result

AMAZON_SENDER_DOMAINS = ["amazon.ca", "amazon.com", "amazon.co.uk", "amazon.de", "amazon.fr", "amazon.com.au"]


def settings_from_env():
    return {
        "host": os.environ.get("IMAP_HOST", "imap.mail.me.com"),
        "port": int(os.environ.get("IMAP_PORT", "993")),
        "user": os.environ.get("IMAP_USER", ""),
        "password": os.environ.get("IMAP_PASSWORD", ""),
        "folder": os.environ.get("IMAP_FOLDER", "INBOX"),
    }


def _since_date(conn, lookback_days: int) -> str:
    last = db.kv_get(conn, "imap_last_sync_date")
    if last:
        # Re-scan a few days of overlap; processed_messages de-duplicates.
        d = date.fromisoformat(last) - timedelta(days=3)
    else:
        d = date.today() - timedelta(days=lookback_days)
    return d.strftime("%d-%b-%Y")


def _search_ids(imap, since: str) -> list[bytes]:
    ids = set()
    for domain in AMAZON_SENDER_DOMAINS:
        typ, data = imap.search(None, "SINCE", since, "FROM", f'"@{domain}"')
        if typ == "OK" and data and data[0]:
            ids.update(data[0].split())
    # Also catch forwarded / oddly-addressed mail that mentions an order number.
    typ, data = imap.search(None, "SINCE", since, "SUBJECT", '"Amazon"')
    if typ == "OK" and data and data[0]:
        ids.update(data[0].split())
    return sorted(ids, key=lambda b: int(b))


def sync(conn, *, host, port, user, password, folder="INBOX", lookback_days=400, limit=2000) -> SyncStats:
    stats = SyncStats()
    imap = imaplib.IMAP4_SSL(host, port)
    try:
        imap.login(user, password)
        typ, _ = imap.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Could not open folder {folder!r}")
        since = _since_date(conn, lookback_days)
        ids = _search_ids(imap, since)[-limit:]
        for uid in ids:
            # Use the server's Message-ID header as the stable identifier.
            typ, hdr = imap.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            raw_hdr = hdr[0][1].decode(errors="ignore") if typ == "OK" and hdr and hdr[0] else ""
            msg_id = raw_hdr.split(":", 1)[1].strip().strip("<>") if ":" in raw_hdr else f"{host}:{folder}:{uid.decode()}"
            key = f"imap:{msg_id}"
            if db.is_processed(conn, key):
                stats.bump("skipped")
                continue
            typ, data = imap.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not data or not data[0]:
                stats.bump("error")
                continue
            try:
                result = parse_eml(data[0][1], message_id=key)
                stats.bump(store_result(conn, result, key, "imap"))
            except Exception as exc:  # keep going on one bad message
                db.mark_processed(conn, key, "imap", "error", str(exc)[:300])
                stats.bump("error")
            conn.commit()
        db.kv_set(conn, "imap_last_sync_date", date.today().isoformat())
        conn.commit()
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return stats

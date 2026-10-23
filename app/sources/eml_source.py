"""Import .eml files (e.g. dragged out of Apple Mail or Outlook) from a folder or an upload."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .. import db
from ..parsers.amazon_email import parse_eml
from .common import SyncStats, store_result


def import_bytes(conn, raw: bytes, name: str, stats: SyncStats | None = None) -> str:
    stats = stats if stats is not None else SyncStats()
    key = "eml:" + hashlib.sha1(raw).hexdigest()
    if db.is_processed(conn, key):
        stats.bump("skipped")
        return "skipped"
    try:
        result = parse_eml(raw, message_id=key)
        outcome = store_result(conn, result, key, "eml")
    except Exception as exc:
        db.mark_processed(conn, key, "eml", "error", f"{name}: {exc}"[:300])
        outcome = "error"
    stats.bump(outcome)
    return outcome


def import_folder(conn, folder: Path) -> SyncStats:
    stats = SyncStats()
    for p in sorted(Path(folder).rglob("*.eml")):
        import_bytes(conn, p.read_bytes(), p.name, stats)
    conn.commit()
    return stats

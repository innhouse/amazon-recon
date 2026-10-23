"""Import the Amazon Business *Reconciliation report* CSV.

Amazon Business -> Business Analytics -> Reconciliation -> download CSV.
Columns (2026): Transaction Date, Payment Reference ID, Transaction Type,
Currency, Payment Amount, Account Group, Payment Instrument Type,
Payment Identifier (card last 4, exported as ="3874"), Account User,
Order Date, Order ID, PO Number, Order Status, ...

One line = one card charge (or refund) with the order it belongs to, which is
exactly what bank reconciliation needs.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime

from .. import db
from .common import SyncStats

REQUIRED = {"transaction date", "payment amount", "order id"}


@dataclass
class ReportRow:
    txn_date: str
    payment_ref: str
    is_refund: bool
    currency: str
    amount: float
    card_last4: str | None
    payer: str | None
    order_date: str | None
    order_id: str
    marketplace: str
    instrument: str | None
    account_group: str | None


def _clean(v: str) -> str:
    v = (v or "").strip()
    m = re.fullmatch(r'="?([^"]*)"?', v)       # Excel-style ="3874"
    return m.group(1) if m else v.strip('"')


def _date(v: str) -> str | None:
    v = _clean(v)
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def looks_like_report(header: list[str]) -> bool:
    h = {c.strip().lower() for c in header}
    return REQUIRED.issubset(h)


def parse_report(content: bytes, *, marketplace: str = "amazon.ca") -> tuple[list[ReportRow], list[str]]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows or not looks_like_report(rows[0]):
        raise ValueError("This does not look like an Amazon Business reconciliation report (missing Transaction Date / Payment Amount / Order ID columns)")
    idx = {c.strip().lower(): i for i, c in enumerate(rows[0])}
    get = lambda r, name: _clean(r[idx[name]]) if name in idx and idx[name] < len(r) else ""
    out, errors = [], []
    for n, r in enumerate(rows[1:], start=2):
        try:
            d = _date(get(r, "transaction date"))
            oid = get(r, "order id")
            amt = float(get(r, "payment amount").replace(",", ""))
            if not d or not re.fullmatch(r"\d{3}-\d{7}-\d{7}", oid):
                raise ValueError(f"bad date/order id: {get(r, 'transaction date')!r} {oid!r}")
            ttype = get(r, "transaction type").lower()
            ref = get(r, "payment reference id") or f"{oid}-{d}-{amt:.2f}"
            out.append(ReportRow(
                txn_date=d, payment_ref=ref, is_refund=("refund" in ttype or amt < 0), currency=get(r, "currency") or "CAD",
                amount=abs(amt), card_last4=get(r, "payment identifier")[-4:] or None, payer=get(r, "account user") or None,
                order_date=_date(get(r, "order date")), order_id=oid, marketplace=marketplace,
                instrument=get(r, "payment instrument type") or None, account_group=get(r, "account group") or None))
        except Exception as exc:
            errors.append(f"row {n}: {exc}")
    return out, errors


def import_report(conn, content: bytes, *, marketplace: str = "amazon.ca") -> tuple[SyncStats, list[str]]:
    rows, errors = parse_report(content, marketplace=marketplace)
    stats = SyncStats()
    for row in rows:
        stats.bump(db.upsert_report_charge(conn, row))
    conn.commit()
    return stats, errors

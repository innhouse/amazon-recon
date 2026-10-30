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
SHIPMENT_LEVEL_REQUIRED = {"charge identifier", "payment reference id", "net total", "order id"}


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
    charge_code: str | None = None     # statement descriptor code (shipment-level report only)
    title: str | None = None           # item on this shipment (shipment-level report only)
    quantity: int | None = None


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


def looks_like_shipment_report(header: list[str]) -> bool:
    h = {c.strip().lower() for c in header}
    return SHIPMENT_LEVEL_REQUIRED.issubset(h)


def parse_shipment_report(content: bytes, *, marketplace: str = "amazon.ca") -> tuple[list[ReportRow], list[str]]:
    """'Reconciliation by shipment level' report: one row per shipment (= one card charge = one invoice),
    with the Charge Identifier that appears on the card statement and the item shipped."""
    text = content.decode("utf-8-sig", errors="replace")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows or not looks_like_shipment_report(rows[0]):
        raise ValueError("This does not look like the 'Reconciliation by shipment level' report")
    idx = {c.strip().lower(): i for i, c in enumerate(rows[0])}
    get = lambda r, name: _clean(r[idx[name]]) if name in idx and idx[name] < len(r) else ""
    out, errors = [], []
    for n, r in enumerate(rows[1:], start=2):
        try:
            oid = get(r, "order id")
            if not re.fullmatch(r"[A-Z0-9]{3}-\d{7}-\d{7}", oid):
                raise ValueError(f"bad order id {oid!r}")
            d = _date(get(r, "payment date")) or _date(get(r, "shipment/refund date"))
            amt_s = get(r, "net total")
            if not d or amt_s in ("", "N/A"):
                continue   # not yet charged (pending shipment)
            amt = float(amt_s.replace(",", ""))
            ttype = get(r, "transaction type").lower()
            ref = get(r, "payment reference id")
            code = get(r, "charge identifier")
            if ref in ("", "N/A"):
                ref = f"{oid}-{code or d}-{amt:.2f}"
            qty_s = get(r, "shipment quantity")
            out.append(ReportRow(
                txn_date=d, payment_ref=ref, is_refund=("refund" in ttype or amt < 0), currency=get(r, "currency") or "CAD",
                amount=abs(amt), card_last4=(get(r, "payment identifier")[-4:] or None) if get(r, "payment identifier") not in ("", "N/A") else None,
                payer=get(r, "account user") or None, order_date=_date(get(r, "order date")), order_id=oid, marketplace=marketplace,
                instrument=get(r, "order payment method") or None, account_group=get(r, "account group") or None,
                charge_code=code if code and code != "N/A" else None, title=get(r, "title") or None,
                quantity=int(float(qty_s)) if qty_s not in ("", "N/A") else None))
        except Exception as exc:
            errors.append(f"row {n}: {exc}")
    return out, errors


def import_shipment_report(conn, content: bytes, *, marketplace: str = "amazon.ca") -> tuple[SyncStats, list[str]]:
    rows, errors = parse_shipment_report(content, marketplace=marketplace)
    stats = SyncStats()
    for row in rows:
        outcome = db.upsert_report_charge(conn, row)
        stats.bump(outcome)
        if row.title and not row.is_refund:
            # Item shipped on this charge (replace any earlier guess for this shipment).
            sid = conn.execute("SELECT shipment_id FROM shipments WHERE payment_ref = ? OR shipment_id = ?", (row.payment_ref, row.payment_ref)).fetchone()
            if sid:
                conn.execute("DELETE FROM order_items WHERE shipment_id = ?", (sid[0],))
                conn.execute("INSERT INTO order_items(order_id, shipment_id, description, quantity, unit_price, currency) VALUES (?,?,?,?,?,?)",
                             (row.order_id, sid[0], row.title, row.quantity or 1, None, row.currency))
    conn.commit()
    return stats, errors


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
            if not d or not re.fullmatch(r"[A-Z0-9]{3}-\d{7}-\d{7}", oid):
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

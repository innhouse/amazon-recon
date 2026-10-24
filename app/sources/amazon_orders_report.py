"""Import the Amazon Business *Orders* report CSV (Business Analytics -> Reports -> Orders).

One row per order line item, with the item title, quantity, unit price, taxes,
order totals and the payment reference that ties it to a card charge. It adds
item descriptions to orders that the reconciliation report only knows by number.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from .. import db
from .common import SyncStats

REQUIRED = {"order id", "title", "item quantity"}


def _clean(v: str) -> str:
    v = (v or "").strip()
    m = re.fullmatch(r'="?([^"]*)"?', v)
    return m.group(1) if m else v.strip('"')


def _date(v: str):
    v = _clean(v)
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _num(v: str):
    v = _clean(v).replace(",", "")
    try:
        return float(v)
    except ValueError:
        return None


def looks_like_orders_report(header: list[str]) -> bool:
    h = {c.strip().lower() for c in header}
    return REQUIRED.issubset(h)


def import_orders_report(conn, content: bytes, *, marketplace: str = "amazon.ca") -> tuple[SyncStats, list[str]]:
    text = content.decode("utf-8-sig", errors="replace")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows or not looks_like_orders_report(rows[0]):
        raise ValueError("This does not look like an Amazon Business Orders report (missing Order ID / Title / Item Quantity columns)")
    idx = {c.strip().lower(): i for i, c in enumerate(rows[0])}
    get = lambda r, name: _clean(r[idx[name]]) if name in idx and idx[name] < len(r) else ""
    stats, errors, seen_orders = SyncStats(), [], set()
    for n, r in enumerate(rows[1:], start=2):
        try:
            oid = get(r, "order id")
            if not re.fullmatch(r"\d{3}-\d{7}-\d{7}", oid):
                raise ValueError(f"bad order id {oid!r}")
            currency = get(r, "currency") or "CAD"
            db.ensure_order_stub(conn, oid, marketplace, _date(get(r, "order date")),
                                 f"https://www.{marketplace}/gp/css/summary/print.html?orderID={oid}")
            conn.execute("UPDATE orders SET order_date = COALESCE(order_date, ?), total = COALESCE(?, total), currency = COALESCE(currency, ?), buyer = COALESCE(buyer, ?) WHERE order_id = ?",
                         (_date(get(r, "order date")), _num(get(r, "order net total")), currency, get(r, "account user") or None, oid))
            if oid not in seen_orders:
                # Replace the order-level item list with what the report says.
                conn.execute("DELETE FROM order_items WHERE order_id = ? AND shipment_id IS NULL", (oid,))
                seen_orders.add(oid)
            title = get(r, "title")
            qty = int(float(get(r, "item quantity") or 1))
            # A line is repeated once per payment; only add each title once per order.
            dup = conn.execute("SELECT 1 FROM order_items WHERE order_id = ? AND shipment_id IS NULL AND description = ? AND quantity = ?",
                               (oid, title, qty)).fetchone()
            if not dup and title:
                conn.execute("INSERT INTO order_items(order_id, shipment_id, description, quantity, unit_price, currency) VALUES (?,?,?,?,?,?)",
                             (oid, None, title, qty, _num(get(r, "purchase ppu")), currency))
                stats.bump("items")
            # Link the payment reference to its shipment row if the reconciliation report created one.
            ref = get(r, "payment reference id")
            if ref and ref != "N/A":
                conn.execute("UPDATE shipments SET order_id = ? WHERE shipment_id = ? AND order_id = ?", (oid, ref, oid))
        except Exception as exc:
            errors.append(f"row {n}: {exc}")
    stats["orders"] = len(seen_orders)
    conn.commit()
    return stats, errors

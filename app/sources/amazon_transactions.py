"""Import charges from the text of Amazon's "Your Transactions" page
(Your Account -> Your Payments -> Transactions). Personal accounts have no
downloadable report, but the page can be selected and copied. The text, in
whatever layout the current site uses, is a sequence of lines like:

    October 17, 2026
    Visa ****3874
    -$106.39
    Order #701-5120229-1371409
    Amazon.ca

A charge is emitted every time an order number is seen, using the most recent
date, card and amount above it. Positive amounts (or lines mentioning Refund)
are treated as refunds.
"""
from __future__ import annotations

import re
from datetime import datetime

from .. import db
from .amazon_report import ReportRow
from .common import SyncStats

ORDER_RE = re.compile(r"(\d{3}-\d{7}-\d{7})")
CARD_RE = re.compile(r"(?:\*{2,}|ending in|ending|x{2,})\s*(\d{4})\b", re.I)
AMOUNT_RE = re.compile(r"(?P<sign>[-+−])?\s*(?:CA\$|C\$|CAD|US\$|USD|\$|£|€)\s*(?P<amt>\d{1,3}(?:,\d{3})*\.\d{2})|(?P<sign2>[-+−])?\s*(?P<amt2>\d{1,3}(?:,\d{3})*\.\d{2})\s*(?:CAD|USD|GBP|EUR)\b")
DATE_FORMATS = ["%B %d, %Y", "%b %d, %Y", "%b. %d, %Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"]
SKIP_WORDS = ("pending", "completed", "transactions", "payment method", "filter")


def _date(line: str) -> str | None:
    t = re.sub(r"\s+", " ", line.strip().rstrip(":"))
    t = re.sub(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+", "", t, flags=re.I)
    t = re.sub(r"\bSept\b", "Sep", t)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_transactions_text(text: str, *, marketplace: str = "amazon.ca", card_hint: str | None = None) -> tuple[list[ReportRow], list[str]]:
    rows, notes = [], []
    cur_date = cur_card = None
    cur_amount = None          # signed: negative = charge (as the page shows it)
    cur_refund_hint = False
    currency = "CAD" if marketplace.endswith(".ca") else "USD"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        d = _date(line)
        if d:
            cur_date = d
            continue
        cm = CARD_RE.search(line)
        if cm:
            cur_card = cm.group(1)
            # the same line may also hold the amount; fall through
        am = AMOUNT_RE.search(line)
        if am and not ORDER_RE.search(line):
            amt = float((am.group("amt") or am.group("amt2")).replace(",", ""))
            sign = am.group("sign") or am.group("sign2") or ""
            cur_amount = -amt if sign in ("-", "−") else amt
            cur_refund_hint = "refund" in line.lower()
            if "USD" in line or "US$" in line:
                currency = "USD"
            continue
        om = ORDER_RE.search(line)
        if om:
            oid = om.group(1)
            if cur_date is None or cur_amount is None:
                notes.append(f"order {oid}: missing date or amount above it, skipped")
                continue
            is_refund = cur_amount > 0 or cur_refund_hint or "refund" in line.lower()
            card = cur_card or card_hint
            amount = abs(cur_amount)
            rows.append(ReportRow(txn_date=cur_date, payment_ref=f"TXN-{oid}-{cur_date}-{amount:.2f}-{card or 'x'}-{len(rows)}",
                                  is_refund=is_refund, currency=currency, amount=amount, card_last4=card, payer=None,
                                  order_date=None, order_id=oid, marketplace=marketplace, instrument=None, account_group=None))
            cur_amount = None
            cur_refund_hint = False
    if not rows:
        notes.append("no order numbers found")
    return rows, notes


def import_text(conn, text: str, *, marketplace: str = "amazon.ca", card_hint: str | None = None) -> tuple[SyncStats, list[str]]:
    rows, notes = parse_transactions_text(text, marketplace=marketplace, card_hint=card_hint)
    stats = SyncStats()
    # De-duplicate against earlier pastes of the same page: same order, date, amount, card.
    for row in rows:
        dup = conn.execute(
            """SELECT 1 FROM shipments WHERE order_id = ? AND shipped_date = ? AND ABS(total - ?) < 0.005
               AND COALESCE(card_last4, '') = COALESCE(?, '') AND payment_ref LIKE 'TXN-%'""",
            (row.order_id, row.txn_date, row.amount, row.card_last4)).fetchone()
        if dup and not row.is_refund:
            stats.bump("skipped")
            continue
        stats.bump(db.upsert_report_charge(conn, row))
    conn.commit()
    return stats, notes

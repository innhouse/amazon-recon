"""Match bank transactions to Amazon shipments / orders / refunds.

Amazon charges the card per *shipment*, so a 105.26 order that ships in two
boxes shows up on the statement as, say, 52.63 + 52.63. Matching therefore
runs in this order, for each unmatched Amazon-looking bank line:

  1. exact_shipment  - one shipment with the same amount, shipped 0-10 days before the charge
  2. exact_order     - the order total (single-box orders where the "Shipped" mail was not captured)
  3. shipment_group  - 2-4 shipments of the same order that add up to the charge
  4. exact_refund    - for credits (negative amounts), a refund with the same amount

Each candidate carries a confidence; only unique candidates are auto-accepted.
Ambiguous ones are surfaced for manual review.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import combinations

from .. import config, db


@dataclass
class Candidate:
    kind: str            # shipment | order | refund | shipment_group
    ref_ids: list[str]
    order_id: str
    method: str
    confidence: float
    label: str
    ref_date: str | None


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _within(ref: date | None, txn: date, before: int, after: int) -> bool:
    if ref is None:
        return True
    return ref - timedelta(days=before) <= txn <= ref + timedelta(days=after)


def _amounts_equal(a: float | None, b: float) -> bool:
    return a is not None and abs(round(a, 2) - round(b, 2)) <= config.MATCH_AMOUNT_TOLERANCE + 1e-9


def _date_score(ref: date | None, txn: date) -> float:
    """1.0 when the charge posts on the ship date, decaying with distance. A charge that
    posts *before* the shipment is unusual (time zones only), so it decays much faster."""
    if ref is None:
        return 0.5
    gap = (txn - ref).days
    if gap >= 0:
        return max(0.0, 1.0 - gap / 30.0)
    return max(0.0, 1.0 + gap / 5.0)


CARD_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def card_of(txn) -> str | None:
    """Last four digits of the card, taken from the account label ("RBC Visa 3874")."""
    m = CARD_RE.findall(txn["account"] or "")
    return m[-1] if m else None


def _card_ok(card: str | None, row) -> bool:
    """Reject a candidate when both sides know the card and they differ."""
    other = row["card_last4"] if "card_last4" in row.keys() else None
    return not (card and other) or card == other


def _card_bonus(card: str | None, row) -> float:
    other = row["card_last4"] if "card_last4" in row.keys() else None
    return 0.1 if (card and other and card == other) else 0.0


def candidates_for(conn, txn, *, used_shipments, used_orders, used_refunds, include_all=False) -> list[Candidate]:
    """Compute match candidates for a bank transaction row (sqlite Row or dict)."""
    txn_date = _d(txn["txn_date"])
    out: list[Candidate] = []
    B, A = config.MATCH_SHIPMENT_WINDOW_BEFORE, config.MATCH_SHIPMENT_WINDOW_AFTER
    card = card_of(txn)

    if txn["amount"] is None:
        # Date-only request (accounting sent a list of charge dates without amounts):
        # propose every unmatched shipment that shipped shortly before the charge date.
        for s in conn.execute("SELECT * FROM shipments ORDER BY shipped_date"):
            if s["shipment_id"] in used_shipments or not s["shipped_date"] or not _card_ok(card, s):
                continue
            # Report charges carry the exact posting date; email shipments post a few days later.
            before, after = (0, 0) if s["source"] == "report" and s["card_last4"] else (0, 4)
            if _within(_d(s["shipped_date"]), txn_date, before, after):
                out.append(Candidate("shipment", [s["shipment_id"]], s["order_id"], "date_only",
                                     0.4 + 0.3 * _date_score(_d(s["shipped_date"]), txn_date) + _card_bonus(card, s),
                                     f"Shipment of order {s['order_id']} ({s['total']} {s['currency']})", s["shipped_date"]))
        return sorted(out, key=lambda c: -c.confidence)

    amount = round(float(txn["amount"]), 2)

    if amount < 0:
        for r in conn.execute("SELECT * FROM refunds"):
            if r["refund_id"] in used_refunds or not _card_ok(card, r):
                continue
            if _amounts_equal(r["total"], -amount) and _within(_d(r["refund_date"]), txn_date, 2, 15):
                out.append(Candidate("refund", [r["refund_id"]], r["order_id"], "exact_refund",
                                     0.7 + 0.3 * _date_score(_d(r["refund_date"]), txn_date) + _card_bonus(card, r),
                                     f"Refund for order {r['order_id']}", r["refund_date"]))
        return sorted(out, key=lambda c: -c.confidence)

    shipments = [s for s in conn.execute("SELECT * FROM shipments")
                 if s["shipment_id"] not in used_shipments and _card_ok(card, s)]
    # 1. single shipment
    for s in shipments:
        if _amounts_equal(s["total"], amount) and _within(_d(s["shipped_date"]), txn_date, B, A):
            out.append(Candidate("shipment", [s["shipment_id"]], s["order_id"], "exact_shipment",
                                 0.75 + 0.25 * _date_score(_d(s["shipped_date"]), txn_date) + _card_bonus(card, s),
                                 f"Shipment of order {s['order_id']}", s["shipped_date"]))
    # 2. whole order (only when the order has no shipment-level charges already matched)
    for o in conn.execute("SELECT * FROM orders"):
        if o["order_id"] in used_orders:
            continue
        order_ships = [s for s in conn.execute("SELECT shipment_id, total FROM shipments WHERE order_id = ?", (o["order_id"],))]
        if any(s["shipment_id"] in used_shipments for s in order_ships):
            continue
        if _amounts_equal(o["total"], amount) and _within(_d(o["order_date"]), txn_date, 1, config.MATCH_ORDER_WINDOW_AFTER):
            # If the order has exactly one shipment with the same total we already proposed it above.
            if any(_amounts_equal(s["total"], amount) for s in order_ships):
                continue
            out.append(Candidate("order", [o["order_id"]], o["order_id"], "exact_order",
                                 0.6 + 0.25 * _date_score(_d(o["order_date"]), txn_date),
                                 f"Order {o['order_id']} total", o["order_date"]))
    # 3. groups of shipments from the same order
    by_order: dict[str, list] = {}
    for s in shipments:
        if s["total"] is not None and _within(_d(s["shipped_date"]), txn_date, B, A + 5):
            by_order.setdefault(s["order_id"], []).append(s)
    for oid, ships in by_order.items():
        if len(ships) < 2:
            continue
        for k in (2, 3, 4):
            for combo in combinations(ships, k):
                if _amounts_equal(sum(s["total"] for s in combo), amount):
                    out.append(Candidate("shipment_group", [s["shipment_id"] for s in combo], oid, "shipment_group",
                                         0.55 + 0.2 * min(_date_score(_d(s["shipped_date"]), txn_date) for s in combo),
                                         f"{k} shipments of order {oid}", max(s["shipped_date"] or "" for s in combo)))
    # Deduplicate identical ref sets, best confidence first.
    seen, uniq = set(), []
    for c in sorted(out, key=lambda c: -c.confidence):
        key = (c.kind, tuple(sorted(c.ref_ids)))
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


def run_auto_match(conn, *, include_non_amazon=False) -> dict:
    """Auto-accept unique high-confidence candidates. Returns a summary dict."""
    used_ships = db.matched_ref_ids(conn, "shipment")
    used_orders = db.matched_ref_ids(conn, "order")
    used_refunds = db.matched_ref_ids(conn, "refund")
    where = "" if include_non_amazon else "AND t.is_amazon = 1"
    rows = conn.execute(
        f"""SELECT t.* FROM bank_transactions t LEFT JOIN matches m ON m.bank_txn_id = t.id
            WHERE m.id IS NULL {where} ORDER BY t.txn_date""").fetchall()
    summary = {"matched": 0, "ambiguous": 0, "unmatched": 0}
    for t in rows:
        cands = candidates_for(conn, t, used_shipments=used_ships, used_orders=used_orders, used_refunds=used_refunds)
        if not cands:
            summary["unmatched"] += 1
            continue
        best = cands[0]
        # Accept when there is a single candidate, when the best clearly beats the runner-up,
        # or when every candidate belongs to the same order (same invoice either way, e.g.
        # twelve identical 76.61 charges of one big order on the same day).
        unique = (len(cands) == 1 or (best.confidence - cands[1].confidence) >= 0.15
                  or len({c.order_id for c in cands}) == 1)
        if unique and best.confidence >= 0.6:
            db.save_match(conn, t["id"], best.kind, best.ref_ids, best.order_id, best.method, round(best.confidence, 3), best.label)
            if best.kind in ("shipment", "shipment_group"):
                used_ships.update(best.ref_ids)
            elif best.kind == "order":
                used_orders.update(best.ref_ids)
            else:
                used_refunds.update(best.ref_ids)
            summary["matched"] += 1
        else:
            summary["ambiguous"] += 1
    conn.commit()
    return summary


def review_queue(conn):
    """Unmatched Amazon bank lines with their candidate list, for the manual review page."""
    used_ships = db.matched_ref_ids(conn, "shipment")
    used_orders = db.matched_ref_ids(conn, "order")
    used_refunds = db.matched_ref_ids(conn, "refund")
    rows = conn.execute(
        """SELECT t.* FROM bank_transactions t LEFT JOIN matches m ON m.bank_txn_id = t.id
           WHERE m.id IS NULL AND t.is_amazon = 1 ORDER BY t.txn_date DESC""").fetchall()
    return [(t, candidates_for(conn, t, used_shipments=used_ships, used_orders=used_orders, used_refunds=used_refunds))
            for t in rows]


def unbilled_shipments(conn):
    """Shipments / orders with no bank charge yet (money Amazon may still take, or charges on another card)."""
    used_ships = db.matched_ref_ids(conn, "shipment")
    used_orders = db.matched_ref_ids(conn, "order")
    ships = [s for s in conn.execute("SELECT s.*, o.marketplace FROM shipments s JOIN orders o USING(order_id) ORDER BY shipped_date DESC")
             if s["shipment_id"] not in used_ships and s["order_id"] not in used_orders]
    return ships

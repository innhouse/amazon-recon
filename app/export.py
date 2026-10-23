"""Build the package accounting asks for: a CSV of charge -> order matches plus one PDF per charge."""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path

from . import config
from .invoices.summary_pdf import build_summary_pdf

SUMMARY_DIR = config.DATA_DIR / "summaries"


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "").strip("_")[:40]


def matched_rows(conn, *, account: str | None = None, date_from: str | None = None, date_to: str | None = None):
    q = """SELECT t.id AS txn_id, t.txn_date, t.description, t.amount, t.account,
                  m.kind, m.ref_ids, m.order_id, m.method, m.confidence
           FROM bank_transactions t LEFT JOIN matches m ON m.bank_txn_id = t.id
           WHERE t.is_amazon = 1"""
    args = []
    if account:
        q += " AND t.account = ?"; args.append(account)
    if date_from:
        q += " AND t.txn_date >= ?"; args.append(date_from)
    if date_to:
        q += " AND t.txn_date <= ?"; args.append(date_to)
    q += " ORDER BY t.txn_date, t.id"
    return conn.execute(q, args).fetchall()


def reconciliation_csv(conn, **filters) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["statement_date", "account", "description", "amount", "status", "order_id", "order_date", "order_total",
                "shipment_ids", "items", "match_method", "confidence", "official_invoice_url", "invoice_pdf"])
    for r in matched_rows(conn, **filters):
        if r["order_id"]:
            o = conn.execute("SELECT * FROM orders WHERE order_id = ?", (r["order_id"],)).fetchone()
            refs = json.loads(r["ref_ids"])
            if r["kind"] in ("shipment", "shipment_group"):
                items = conn.execute(f"SELECT description, quantity FROM order_items WHERE shipment_id IN ({','.join('?'*len(refs))})", refs).fetchall()
            else:
                items = conn.execute("SELECT description, quantity FROM order_items WHERE order_id = ?", (r["order_id"],)).fetchall()
            w.writerow([r["txn_date"], r["account"], r["description"], r["amount"], "matched", r["order_id"], o["order_date"],
                        o["total"], ";".join(refs) if r["kind"] != "order" else "", " | ".join(f"{i['quantity']}x {i['description'][:60]}" for i in items),
                        r["method"], r["confidence"], o["invoice_url"], o["invoice_pdf"]])
        else:
            w.writerow([r["txn_date"], r["account"], r["description"], r["amount"], "UNMATCHED", "", "", "", "", "", "", "", "", ""])
    return buf.getvalue()


def build_package(conn, *, account: str | None = None, date_from: str | None = None, date_to: str | None = None) -> tuple[bytes, dict]:
    """Zip with the CSV plus one PDF per matched charge (official if downloaded, else generated summary)."""
    stats = {"charges": 0, "matched": 0, "official_pdfs": 0, "summary_pdfs": 0}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("reconciliation.csv", reconciliation_csv(conn, account=account, date_from=date_from, date_to=date_to))
        for r in matched_rows(conn, account=account, date_from=date_from, date_to=date_to):
            stats["charges"] += 1
            if not r["order_id"]:
                continue
            stats["matched"] += 1
            o = conn.execute("SELECT * FROM orders WHERE order_id = ?", (r["order_id"],)).fetchone()
            amt = f"{r['amount']:.2f}" if r["amount"] is not None else "unknown"
            base = f"{r['txn_date']}_{amt}_{o['order_id']}"
            if o["invoice_pdf"] and Path(o["invoice_pdf"]).exists():
                z.write(o["invoice_pdf"], f"invoices/{base}_official.pdf")
                stats["official_pdfs"] += 1
            else:
                refs = json.loads(r["ref_ids"])
                ships = conn.execute("SELECT * FROM shipments WHERE order_id = ? ORDER BY shipped_date", (o["order_id"],)).fetchall()
                if r["kind"] in ("shipment", "shipment_group"):
                    ships = [s for s in ships if s["shipment_id"] in refs]
                items = conn.execute("SELECT * FROM order_items WHERE order_id = ?", (o["order_id"],)).fetchall()
                txn = conn.execute("SELECT * FROM bank_transactions WHERE id = ?", (r["txn_id"],)).fetchone()
                p = build_summary_pdf(SUMMARY_DIR / f"{base}_summary.pdf", order=o, shipments=ships, items=items, bank_txn=txn)
                z.write(p, f"invoices/{base}_summary.pdf")
                stats["summary_pdfs"] += 1
    return buf.getvalue(), stats
